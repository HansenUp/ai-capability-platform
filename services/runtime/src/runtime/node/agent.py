"""节点代理（S9.5 / S9.8，D144 / D148；冻结契约 ⑤ ``runtime.node.agent.start(app)``）。

节点只出不进：代理以 ``ai-node-svc`` 服务令牌（client credentials，``NODE_TOKEN_URL``）主动访问 Console，每个周期
（``NODE_POLL_INTERVAL_S``）依次：心跳（Console 返回 404 即重新注册）→ 经能力网关刷新 Skill 吊销列表 → 拉取指令
``GET /nodes/{id}/commands``（拉取不清空队列）→ 逐条执行并回执 ``POST /nodes/{id}/commands/{cid}/ack``。

指令：``reload_skills{skills:[{name, version, manifest_sha256}]}`` → 逐个 ``runtime.skills.store.materialize``（下载 → 验签 →
原子物化）；``reload_config{poll_interval_s?, timeout_s?}`` 仅调整本代理的轮询与超时，禁止修改鉴权或 Runtime 配置。
``switch_model`` 只选择已发布的不可变模型版本；``graceful_restart`` 只在受管节点排空、持久回执后退出。
未知类型回执 ``unsupported``。回执失败时停止处理后续指令，已执行的指令只重发回执、不重复执行。
未配置 ``CONSOLE_URL`` 时不启动（单机开发）。开发态未配令牌端点时使用 ``NODE_CONSOLE_TOKEN`` 静态令牌。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from dataclasses import replace
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.exc import SQLAlchemyError

from runtime import net
from runtime.config import settings
from runtime.node import settings as node_settings
from runtime.node.lifecycle import NodeLifecycle
from runtime.skills import store
from runtime.skills.verify import SkillVerifyError

log = logging.getLogger(__name__)
TOKEN_REFRESH_MARGIN_S = 30.0
NODE_VERSION = "0.1.0"


class ReloadNodeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    poll_interval_s: float | None = Field(default=None, ge=1, le=3600, allow_inf_nan=False)
    timeout_s: float | None = Field(default=None, ge=0.1, le=120, allow_inf_nan=False)


class GracefulRestart(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    timeout_s: float = Field(default=30, ge=0.1, le=300, allow_inf_nan=False)


_transport: httpx.AsyncBaseTransport | None = None  # 测试注入（Console 与令牌端点请求都经它）


def set_transport_for_tests(transport: httpx.AsyncBaseTransport | None) -> None:
    global _transport
    _transport = transport


class NodeAuthError(httpx.HTTPError):
    """节点服务令牌换取失败。"""


class NodeAgent:
    def __init__(
        self,
        ns: node_settings.NodeSettings | None = None,
        node_id: str | None = None,
        lifecycle: NodeLifecycle | None = None,
        *,
        serves_requests: bool = True,
    ) -> None:
        self.ns = ns or node_settings.load()
        self.node_id = node_id or settings.node_id
        self._token: tuple[str, float] | None = None
        self._done: dict[str, dict[str, Any]] = {}  # 已执行指令 → 回执体
        self._task: asyncio.Task[None] | None = None
        self.registered = False
        self.lifecycle = lifecycle
        self._restart_id: str | None = None
        self.boot_id = uuid.uuid4().hex
        self.serves_requests = serves_requests

    # ---- 鉴权 ----
    async def _bearer(self) -> str | None:
        ns = self.ns
        if not ns.token_url:
            return ns.static_token
        if self._token and time.monotonic() < self._token[1]:
            return self._token[0]
        if not ns.client_secret:
            raise NodeAuthError("NODE_CLIENT_SECRET is not configured")
        data = {"grant_type": "client_credentials", "client_id": ns.client_id, "client_secret": ns.client_secret}
        async with net.client(timeout=ns.timeout_s, transport=_transport) as c:
            r = await c.post(ns.token_url, data=data)
        if r.status_code != 200:
            raise NodeAuthError(f"node token request rejected: HTTP {r.status_code}")
        try:
            body = r.json()
            token, ttl = str(body["access_token"]), float(body.get("expires_in", 60))
        except (ValueError, KeyError, TypeError) as e:
            raise NodeAuthError("node token response malformed") from e
        self._token = (token, time.monotonic() + max(ttl - TOKEN_REFRESH_MARGIN_S, 1.0))
        return token

    async def _call(self, method: str, path: str, json: Any = None) -> httpx.Response:
        token = await self._bearer()
        hdrs = {"Authorization": f"Bearer {token}"} if token else {}
        url = f"{(self.ns.console_url or '').rstrip('/')}{path}"
        async with net.client(timeout=self.ns.timeout_s, transport=_transport) as c:
            r = await c.request(method, url, json=json, headers=hdrs)
        if r.status_code == 401:
            self._token = None  # 令牌可能已被吊销：下次重新换取
        return r

    # ---- 周期动作 ----
    async def register(self) -> None:
        body = {
            "node_id": self.node_id,
            "topology": settings.topology,
            "labels": list(self.ns.labels),
            "version": NODE_VERSION,
            "ready": self.lifecycle is None or self.lifecycle.accepting,
            "boot_id": self.boot_id,
        }
        r = await self._call("POST", "/nodes/register", body)
        r.raise_for_status()
        self.registered = True

    async def heartbeat(self) -> None:
        ready = self.lifecycle is None or self.lifecycle.accepting
        r = await self._call("POST", f"/nodes/{self.node_id}/heartbeat", {"load": 0, "qps": 0, "version": NODE_VERSION, "ready": ready})
        if r.status_code == 404:
            self.registered = False
            await self.register()
            r = await self._call("POST", f"/nodes/{self.node_id}/heartbeat", {"load": 0, "qps": 0, "version": NODE_VERSION, "ready": ready})
        r.raise_for_status()

    async def execute(self, cmd: dict[str, Any]) -> dict[str, Any]:
        """执行一条指令，返回回执体 {status: ok|error|unsupported, detail}。"""
        kind, args = cmd.get("type"), cmd.get("args", {})
        if kind == "switch_model":
            if not self.serves_requests:
                return {"status": "error", "detail": {"code": "MODEL_SWITCH_REQUIRES_API_NODE"}}
            from runtime import llm
            from runtime.node.models import switch

            try:
                return await switch(self.node_id, cmd)
            except (ValueError, llm.LLMError, llm.UnknownModelAlias, httpx.HTTPError):
                return {"status": "error", "detail": {"code": "SWITCH_MODEL_REJECTED"}}
        if kind == "graceful_restart" and self.lifecycle is not None:
            if cmd.get("authorization_role") != "PLATFORM_ADMIN" or not isinstance(cmd.get("id"), str) or not cmd["id"]:
                return {"status": "error", "detail": {"code": "RESTART_DENIED"}}
            try:
                restart_config = GracefulRestart.model_validate(args)
                await self.lifecycle.prepare(restart_config.timeout_s, self.heartbeat)
            except (ValueError, TimeoutError, httpx.HTTPError):
                self.lifecycle.resume()
                with contextlib.suppress(httpx.HTTPError):
                    await self.heartbeat()
                return {"status": "error", "detail": {"code": "RESTART_NOT_DRAINED"}}
            self._restart_id = cmd["id"]
            return {
                "status": "ok",
                "detail": {"phase": "drained_restart_requested", "completion_requires_new_process": True, "boot_id": self.boot_id},
            }
        if kind == "reload_config":
            try:
                config = ReloadNodeConfig.model_validate(args)
                changes = config.model_dump(exclude_unset=True)
                if any(value is None for value in changes.values()):
                    raise ValueError("configuration values cannot be null")
                updated = replace(self.ns, **changes)
            except (ValidationError, ValueError, TypeError):
                return {"status": "error", "detail": {"code": "BAD_ARGS", "scope": "node_agent"}}
            self.ns = updated
            return {
                "status": "ok",
                "detail": {"scope": "node_agent", "poll_interval_s": updated.poll_interval_s, "timeout_s": updated.timeout_s},
            }
        if kind != "reload_skills":
            return {"status": "unsupported", "detail": {"type": kind}}
        skills = args.get("skills") if isinstance(args, dict) else None
        if not isinstance(skills, list):
            return {"status": "error", "detail": {"error": "args.skills must be a list"}}
        results: list[dict[str, Any]] = []
        for s in skills:
            try:
                name, version, sha = str(s["name"]), str(s["version"]), str(s["manifest_sha256"])
            except (KeyError, TypeError):
                results.append({"skill": s, "status": "error", "code": "BAD_ARGS"})
                continue
            try:
                await store.materialize(name, version, sha)
                results.append({"name": name, "version": version, "status": "ok"})
            except SkillVerifyError as e:
                results.append({"name": name, "version": version, "status": "error", "code": e.code, "message": e.message})
        ok = all(r["status"] == "ok" for r in results)
        return {"status": "ok" if ok else "error", "detail": {"skills": results}}

    async def poll_commands(self) -> list[dict[str, Any]]:
        """拉取并执行指令、逐条回执；返回本轮处理的回执列表。"""
        r = await self._call("GET", f"/nodes/{self.node_id}/commands")
        r.raise_for_status()
        commands = r.json()
        if (
            self._restart_id is not None
            and self.lifecycle is not None
            and not any(command.get("id") == self._restart_id for command in commands)
        ):
            receipt = await self._call("GET", f"/nodes/{self.node_id}/commands/{self._restart_id}/receipt")
            if (
                receipt.status_code == 200
                and receipt.json().get("status") == "ok"
                and receipt.json().get("result") == self._done.get(self._restart_id, {}).get("detail")
            ):
                self.lifecycle.commit()
                return []
            if receipt.status_code == 404 or (receipt.status_code == 200 and receipt.json().get("status") != "pending"):
                self.lifecycle.resume()
                self._done.pop(self._restart_id, None)
                self._restart_id = None
                await self.heartbeat()
            else:
                return []
        out: list[dict[str, Any]] = []
        for cmd in commands:
            cid = str(cmd.get("id") or "")
            if not cid:
                continue
            ack = self._done.get(cid)
            if ack is None:
                ack = await self.execute(cmd)
                self._done[cid] = ack
            resp = await self._call("POST", f"/nodes/{self.node_id}/commands/{cid}/ack", ack)
            if cid == self._restart_id and self.lifecycle is not None:
                if 200 <= resp.status_code < 300:
                    persisted = resp.json()
                    if persisted.get("status") == "ok" and persisted.get("result") == ack["detail"]:
                        self.lifecycle.commit()
                        return [{"id": cid} | ack]
                    self.lifecycle.resume()
                    self._restart_id = None
                    await self.heartbeat()
                if resp.status_code == 404:
                    self.lifecycle.resume()
                    self._restart_id = None
                    self._done.pop(cid, None)
                    await self.heartbeat()
            if resp.status_code < 300 or resp.status_code == 404:
                self._done.pop(cid, None)  # 已回执（或 Console 已无此指令）：不再记着
            out.append({"id": cid} | ack)
            if resp.status_code >= 300 and resp.status_code != 404:
                break
        return out

    async def tick(self) -> None:
        if not self.registered:
            await self.register()
        await self.heartbeat()
        await store.revocations.refresh()  # 吊销随心跳刷新（失败保留旧列表；组装时按最大沿用时长失败关闭）
        await self.poll_commands()

    async def run_forever(self) -> None:
        while True:
            try:
                await self.tick()
            except (httpx.HTTPError, ValueError, TypeError, KeyError, SQLAlchemyError) as e:
                log.warning("node agent tick failed: %s", type(e).__name__)
            await asyncio.sleep(self.ns.poll_interval_s)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self.run_forever(), name="node-agent")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None


async def start(app: Any = None) -> NodeAgent | None:
    """供 main.py 的 lifespan 挂载：配置了 CONSOLE_URL 时启动节点代理后台任务并返回（关停时 ``await agent.stop()``）；
    否则返回 None。传入 FastAPI app 时同时挂到 ``app.state.node_agent``。"""
    ns = node_settings.load()
    problems = node_settings.problems(ns, settings.runtime_env)
    if problems:
        raise RuntimeError("节点代理配置不满足：" + "；".join(problems))
    if not ns.console_url:
        return None
    state = getattr(app, "state", None)
    agent = NodeAgent(ns, lifecycle=getattr(state, "node_lifecycle", None), serves_requests=getattr(state, "node_serves_requests", True))
    agent.start()
    if app is not None:
        app.state.node_agent = agent
    return agent
