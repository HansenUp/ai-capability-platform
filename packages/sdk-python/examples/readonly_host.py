"""Executable synthetic host: no URL/token options and no real HTTP transport."""

from __future__ import annotations

import argparse
import asyncio
import json
from contextlib import aclosing
from typing import Any
from uuid import uuid4

import httpx

from ai_platform_sdk import PlatformClient
from ai_platform_sdk.workbench import WorkbenchReader

SCENARIOS = ("read", "locked", "disconnect", "foreign")


async def run_demo(scenario: str) -> dict[str, Any]:
    if scenario not in SCENARIOS:
        raise ValueError("Unknown synthetic scenario")
    synthetic_bearer = uuid4().hex
    calls: list[str] = []
    scope = "a" * 64
    identity = {"protocol": "workbench-v1", "auth_method": "user", "scope_id": scope, "tenant_id": "synthetic", "subject": "human:example"}
    receipt = {
        "scope_id": scope,
        "attempt_id": "11111111-1111-4111-8111-111111111111",
        "action": "credential",
        "run_id": "demo-run",
        "request_seq": 1,
        "fingerprint": "b" * 64,
        "status": "PREPARED",
        "revision": 1,
        "evidence_seq": None,
        "rejection": None,
        "acknowledged": False,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method != "GET" or request.url.host != "synthetic.invalid":
            raise AssertionError("Only synthetic GET requests are allowed")
        if request.headers.get("authorization") != "Bearer " + synthetic_bearer:
            raise AssertionError("Synthetic host identity mismatch")
        calls.append(request.url.path)
        if request.url.path == "/v1/workbench/identity":
            return httpx.Response(200, json=identity)
        if request.url.path == "/v1/workbench/attempts":
            return httpx.Response(200, json={"scope_id": scope, "active": receipt if scenario == "locked" else None})
        if request.url.path == "/v1/runs/demo-run/stream":
            if scenario == "foreign":
                return httpx.Response(403, text="synthetic denied")
            event = {
                "run_id": "demo-run",
                "seq": 1,
                "type": "run.state_changed",
                "payload": {"state": "input-required"},
                "ts": "2026-10-29T00:00:00Z",
            }
            body = "id: 1\nevent: run.state_changed\ndata: " + json.dumps(event) + "\n\n"
            if scenario != "disconnect":
                body += 'event: done\ndata: {"type":"done","run_id":"demo-run","stop_reason":"awaiting"}\n\n'
            return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})
        raise AssertionError("Unexpected synthetic route")

    reader = WorkbenchReader("https://synthetic.invalid", synthetic_bearer, transport=httpx.MockTransport(handler))
    client = PlatformClient("https://synthetic.invalid", token=synthetic_bearer, transport=httpx.MockTransport(handler))
    result: dict[str, Any] = {
        "source": "HTTP pure substitute, not real identity acceptance",
        "writes_enabled": False,
        "run_completion": "unknown",
        "events": [],
        "last_seq": 0,
    }
    try:
        verified, active = await reader.ledger()
        result["identity"] = {"tenant": verified.tenant_id, "subject": verified.subject}
        result["pending_write"] = active.status if active else None
        result["lock_guidance"] = (
            "Keep original attempt; explicit owner recovery/ACK elsewhere"
            if active
            else "Empty ledger does not resolve a previous unknown write"
        )
        try:
            async with aclosing(client.follow_run("demo-run")) as stream:
                async for event in stream:
                    result["events"].append({"seq": event.seq, "type": event.type.value})
                    result["last_seq"] = event.seq
            result["read_status"] = "stream ended; no Run completion or unlock inferred"
        except (ValueError, httpx.HTTPError):
            result["read_status"] = "unavailable or disconnected; no automatic reconnect"
            # Partial events are historical observations, not a successful current snapshot.
            result["events"] = []
        return result
    finally:
        await reader.aclose()
        await client.aclose()
        result["requests"] = calls
        result["clients_closed"] = True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=SCENARIOS, default="read")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run_demo(args.scenario)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
