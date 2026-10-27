"""独立本机认证反馈通道；旧入口不自动升级为可信。"""

import hmac
import json
import os
import re
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictInt

router = APIRouter(prefix="/internal/local-feedback", tags=["Local feedback protocol"])
_transport: httpx.AsyncBaseTransport | None = None


def authorize(authorization: Annotated[str | None, Header()] = None) -> None:
    if os.environ.get("OBSERVE_ENV", "dev") != "dev" or os.environ.get("FEEDBACK_LOCAL_PROTOCOL") != "1":
        raise HTTPException(503, "local feedback protocol disabled")
    secret = os.environ.get("FEEDBACK_LOCAL_SERVICE_TOKEN", "")
    if len(secret) < 32 or not authorization or not hmac.compare_digest(authorization.encode(), ("Bearer " + secret).encode()):
        raise HTTPException(401, "feedback service identity required")


class Score(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^fb_[a-f0-9]{32}$")
    name: Literal["user_feedback"]
    dataType: Literal["NUMERIC"]
    traceId: str = Field(min_length=1, max_length=128)
    timestamp: str = Field(min_length=20, max_length=40)
    value: StrictInt = Field(ge=-1, le=1)
    metadata: dict[str, Any]


async def forward(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    host = os.environ.get("LANGFUSE_HOST", "")
    parsed = urlsplit(host)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise HTTPException(503, "isolated Langfuse profile required")
    public, secret = os.environ.get("LANGFUSE_PUBLIC_KEY"), os.environ.get("LANGFUSE_SECRET_KEY")
    if not public or not secret:
        raise HTTPException(503, "Langfuse credentials unavailable")
    try:
        async with httpx.AsyncClient(
            base_url=host.rstrip("/"), auth=(public, secret), timeout=5, follow_redirects=False, trust_env=False, transport=_transport
        ) as client:
            async with client.stream(method, path, json=body) as response:
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 1024 * 1024:
                        raise ValueError
                if response.status_code != 200:
                    raise HTTPException(502, "feedback source unavailable")
                return json.loads(raw)
    except (httpx.HTTPError, ValueError):
        raise HTTPException(502, "feedback source unavailable") from None


@router.post("", dependencies=[Depends(authorize)])
async def deliver(body: Score) -> Any:
    return await forward("POST", "/api/public/scores", body.model_dump())


@router.get("/{score_id}", dependencies=[Depends(authorize)])
async def read(score_id: Annotated[str, Field(pattern=r"^fb_[a-f0-9]{32}$")]) -> Any:
    score = await forward("GET", "/api/public/v2/scores/" + score_id)
    trace_id = score.get("traceId") if isinstance(score, dict) else None
    if not isinstance(trace_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", trace_id):
        raise HTTPException(502, "feedback Trace unavailable")
    trace = await forward("GET", "/api/public/traces/" + trace_id)
    return {"score": score, "trace": trace}
