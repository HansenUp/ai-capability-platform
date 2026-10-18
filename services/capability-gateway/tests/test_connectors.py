"""连接器（S8.5）：登记 / 版本 / 激活、探活（manifest 哈希、写规范校验、连续 3 次失败 down → /invoke 503、恢复）；
写规范三处校验之 validate_tool 与 POST /capabilities 409。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from gateway_testkit import CONNECTOR_URL, TICKET_MANIFEST, Upstream

from capability_gateway import connectors, main, spec, state
from capability_gateway.catalog import ConnectorVersion
from capability_gateway.registry import Capability

c = TestClient(main.app)
REG = {"name": "mcp-ticket", "endpoint": CONNECTOR_URL, "protocol": "rest", "system": "ticket", "version": "2"}


def test_validate_tool_rules() -> None:
    m = [dict(t) for t in TICKET_MANIFEST]
    assert all(spec.validate_tool(t, m) == [] for t in m)  # reopen_batch 是 close_batch 的补偿工具：免于再声明补偿
    bad = {"name": "w", "risk_level": "write_low"}
    errs = spec.validate_tool(bad, [*m, bad])
    assert len(errs) == 3 and any("idempotency" in e for e in errs) and any("dry_run" in e for e in errs)
    orphan = {"name": "w", "risk_level": "write_low", "supports_dry_run": True, "requires_idempotency_key": True, "compensate_tool": "x"}
    assert spec.validate_tool(orphan, [orphan]) == ["w: compensate tool 'x' is not in the same manifest"]
    mcp_style = {"name": "r", "annotations": {"readOnlyHint": True}}
    assert spec.validate_tool(mcp_style, [mcp_style]) == []
    assert spec.validate_tool({"name": "nometa"}, [{"name": "nometa"}])  # 无元数据按非只读处理（失败关闭）
    meta = {
        "name": "w",
        "_meta": {"risk_level": "write_low", "supports_dry_run": True, "requires_idempotency_key": True, "compensate_tool": "w2"},
    }
    assert spec.validate_tool(meta, [meta, {"name": "w2", "risk_level": "read"}]) == []


async def test_register_probe_and_activate(up: Upstream) -> None:
    r = c.post("/mcp/servers", json=REG | {"name": "mcp-new"})
    assert r.status_code == 201 and r.json()["status"] == "active" and r.json()["protocol"] == "rest"
    r = c.post("/mcp/servers/mcp-new/probe")
    body = r.json()
    assert (
        body["health"] == "up"
        and len(body["manifest_hash"]) == 64
        and {t["name"] for t in body["tools"]} == {t["name"] for t in TICKET_MANIFEST}
    )
    assert body["manifest_hash"] == connectors.manifest_hash(list(reversed(TICKET_MANIFEST)))  # 与顺序无关
    # 新版本不自动激活；不合规 manifest 的版本激活 409
    assert c.put("/mcp/servers/mcp-new/versions/3", json={"endpoint": CONNECTOR_URL, "protocol": "rest"}).json()["status"] == "inactive"
    up.manifest.append({"name": "ticket.delete", "risk_level": "write_high"})
    r = c.post("/mcp/servers/mcp-new/versions/3/activate")
    assert r.status_code == 409 and r.json()["code"] == "tool_spec_violation" and "ticket.delete" in r.json()["detail"]
    up.manifest.pop()
    r = c.post("/mcp/servers/mcp-new/versions/3/activate")
    assert r.status_code == 200 and r.json()["status"] == "active"
    versions = c.get("/mcp/servers/mcp-new").json()
    assert [(v["version"], v["status"]) for v in versions] == [("2", "inactive"), ("3", "active")]
    up.connector_down = True
    r = c.post("/mcp/servers/mcp-new/versions/2/activate")
    assert r.status_code == 409 and r.json()["code"] == "connector_unreachable"
    assert c.post("/mcp/servers/nope/probe").status_code == 404


async def test_three_consecutive_failures_mark_down_then_recover(up: Upstream, ticket_connector: object) -> None:
    body = {"tool": "ticket.get", "input": {"id": "T-1"}, "subject": "human:u1", "agent_id": "a", "agent_version": "1"}
    up.connector_down = True
    for i in range(1, 4):
        await connectors.probe_all()
        cv = await state.catalog.active_connector("mcp-ticket")
        assert cv is not None and cv.consecutive_failures == i and cv.health == ("down" if i == 3 else "unknown")
    r = c.post("/invoke", json=body)
    assert r.status_code == 503 and r.json()["code"] == "connector_down"
    up.connector_down = False
    await connectors.probe_all()
    cv = await state.catalog.active_connector("mcp-ticket")
    assert cv is not None and cv.health == "up" and cv.consecutive_failures == 0 and cv.last_error is None
    assert c.post("/invoke", json=body).status_code == 200


async def test_post_capability_enforces_write_spec(up: Upstream, ticket_connector: object) -> None:
    """POST /capabilities：缺幂等 / dry-run / 补偿、控制字段暴露给模型、补偿工具不在同一 manifest → 409。"""
    w = Capability(
        id="cap.ticket.bulk", name="ticket.close_batch", description="d", system="ticket", connector="mcp-ticket", risk_level="write_high"
    )
    r = c.post("/capabilities", json=w.model_dump())
    assert r.status_code == 409 and r.json()["code"] == "tool_spec_violation"
    assert "idempotency" in r.json()["detail"] and "dry_run" in r.json()["detail"] and "compensate" in r.json()["detail"]
    ok = w.model_copy(
        update={
            "id": "cap.ticket.close_batch",
            "supports_dry_run": True,
            "requires_idempotency_key": True,
            "compensate_tool": "ticket.reopen_batch",
        }
    )
    exposed = ok.model_copy(update={"input_schema": {"type": "object", "properties": {"ids": {}, "idempotency_key": {}}}})
    r = c.post("/capabilities", json=exposed.model_dump())
    assert r.status_code == 409 and "idempotency_key" in r.json()["detail"]
    r = c.post("/capabilities", json=ok.model_dump())  # 承载连接器从未探活成功（无 manifest）：不能抢先登记
    assert r.status_code == 409 and "probed manifest" in r.json()["detail"]
    await connectors.probe_all()  # 有 manifest 后：补偿工具必须在同一 manifest，工具本身也必须在 manifest 中
    assert c.post("/capabilities", json=ok.model_dump()).status_code == 201
    r = c.post("/capabilities", json=ok.model_copy(update={"compensate_tool": "other.undo"}).model_dump())
    assert r.status_code == 409 and "same connector manifest" in r.json()["detail"]
    r = c.post("/capabilities", json=Capability(id="cap.ticket.x", name="ticket.x", description="d", connector="mcp-ticket").model_dump())
    assert r.status_code == 409 and "not in the connector manifest" in r.json()["detail"]
    assert c.post("/capabilities", json=ok.model_dump()).json()["row_version"] == 3  # 种子 1 → 两次登记
    dup = Capability(id="cap.dup", name="ticket.search", description="d", system="ticket")
    r = c.post("/capabilities", json=dup.model_dump())
    assert r.status_code == 409 and r.json()["code"] in ("name_conflict", "tool_spec_violation")


def test_bindings_api() -> None:
    assert c.put("/agents/a1/bindings/cap.ticket.get", params={"kind": "discoverable"}).json()["kind"] == "discoverable"
    assert c.put("/agents/a1/bindings/no.such").status_code == 404
    assert [b["capability_id"] for b in c.get("/agents/a1/bindings").json()] == ["cap.ticket.get"]
    assert c.delete("/agents/a1/bindings/cap.ticket.get", params={"kind": "discoverable"}).status_code == 204
    assert c.delete("/agents/a1/bindings/cap.ticket.get", params={"kind": "discoverable"}).status_code == 404


@pytest.mark.parametrize("sse", [False, True])
async def test_mcp_protocol_probe_and_invoke(up: Upstream, sse: bool) -> None:
    """MCP 协议连接器：探活走 initialize + tools/list，/invoke 走 tools/call（JSON 或 SSE 应答）。"""
    import json as _json

    import httpx

    calls: list[dict[str, object]] = []

    def mcp(request: httpx.Request) -> httpx.Response:
        if request.url.host != "mcp.test":
            return up(request)
        msg = _json.loads(request.content)
        calls.append(msg)
        if "id" not in msg:
            return httpx.Response(202)
        result = {"tools": TICKET_MANIFEST} if msg["method"] == "tools/list" else {"protocolVersion": "x"}
        if msg["method"] == "tools/call":
            result = {"structuredContent": {"echo": msg["params"]}, "isError": False}
        payload = {"jsonrpc": "2.0", "id": msg["id"], "result": result}
        if sse:
            return httpx.Response(
                200, headers={"content-type": "text/event-stream", "mcp-session-id": "s1"}, text=f"data: {_json.dumps(payload)}\n\n"
            )
        return httpx.Response(200, headers={"mcp-session-id": "s1"}, json=payload)

    main.set_invoke_transport_for_tests(httpx.MockTransport(mcp))
    await state.catalog.put_connector(ConnectorVersion(name="mcp-ticket", endpoint="http://mcp.test/mcp", protocol="mcp", system="ticket"))
    active = await state.catalog.active_connector("mcp-ticket")
    assert active is not None
    cv, tools = await connectors.probe(active)
    assert cv.health == "up" and tools is not None and len(tools) == len(TICKET_MANIFEST)
    r = c.post("/invoke", json={"tool": "ticket.get", "input": {"id": "T-1"}, "subject": "u", "agent_id": "a", "agent_version": "1"})
    assert r.json()["ok"] is True and r.json()["output"]["echo"] == {"name": "ticket.get", "arguments": {"id": "T-1"}}
    assert [m.get("method") for m in calls][-3:] == ["initialize", "notifications/initialized", "tools/call"]


WRITE = Capability(
    id="cap.new.write",
    name="ticket.close_batch",
    description="d",
    tenant="t-new",
    system="newsys",
    connector="mcp-evil",
    risk_level="write_high",
    supports_dry_run=True,
    requires_idempotency_key=True,
    compensate_tool="ticket.reopen_batch",
)


async def test_register_unprobed_connector_stays_inactive_and_blocks_capability(up: Upstream) -> None:
    """评审 #1：登记后抢在探活前登记写能力 → 409。登记新连接器不可达 / 不合规时保持 inactive，
    POST /capabilities 因承载连接器无已探活的 manifest 拒绝。"""
    up.connector_down = True
    r = c.post("/mcp/servers", json=REG | {"name": "mcp-evil", "system": "newsys", "version": "1"})
    assert r.status_code == 201 and r.json()["status"] == "inactive" and r.json()["manifest_hash"] is None
    r = c.post("/capabilities", json=WRITE.model_dump())
    assert r.status_code == 409 and r.json()["code"] == "tool_spec_violation" and "probed manifest" in r.json()["detail"]
    # manifest 有不合规工具：登记同样保持 inactive
    up.connector_down = False
    up.manifest.append({"name": "ticket.delete", "risk_level": "write_high"})
    r = c.post("/mcp/servers", json=REG | {"name": "mcp-evil", "system": "newsys", "version": "1"})
    assert r.status_code == 201 and r.json()["status"] == "inactive"
    assert c.post("/capabilities", json=WRITE.model_dump()).status_code == 409
    up.manifest.pop()
    r = c.post("/mcp/servers/mcp-evil/versions/1/activate")
    assert r.status_code == 200 and r.json()["activated_manifest_hash"] == r.json()["manifest_hash"]
    assert c.post("/capabilities", json=WRITE.model_dump()).status_code == 201
    bad_comp = WRITE.model_copy(update={"id": "cap.new.w2", "name": "ticket.reopen_batch", "compensate_tool": "nope.undo"})
    r = c.post("/capabilities", json=bad_comp.model_dump())
    assert r.status_code == 409 and "same connector manifest" in r.json()["detail"]


async def test_active_version_is_immutable(up: Upstream) -> None:
    """评审 #1：active 版本的 endpoint 不能经 PUT 版本或重复登记改写（否则绕过激活时的探活与规范校验）。"""
    r = c.post("/mcp/servers", json=REG | {"name": "mcp-new"})
    assert r.json()["status"] == "active"
    r = c.put("/mcp/servers/mcp-new/versions/2", json={"endpoint": "http://evil.test", "protocol": "rest"})
    assert r.status_code == 409 and r.json()["code"] == "version_active"
    r = c.post("/mcp/servers", json=REG | {"name": "mcp-new", "endpoint": "http://evil.test"})
    assert r.status_code == 409 and r.json()["code"] == "version_active"
    assert all(v["endpoint"] == CONNECTOR_URL for v in c.get("/mcp/servers/mcp-new").json())
    assert c.put("/mcp/servers/mcp-new/versions/3", json={"endpoint": "http://evil.test", "protocol": "rest"}).status_code == 200


async def test_invoke_rejects_manifest_drift_and_unlisted_tool(up: Upstream, ticket_connector: object) -> None:
    """评审 #1：/invoke 前检查工具在最近一次 manifest 中且合规、manifest 与激活基线一致。"""
    body = {"tool": "ticket.get", "input": {"id": "T-1"}, "subject": "human:u1", "agent_id": "a", "agent_version": "1"}
    assert c.post("/invoke", json=body).status_code == 200  # 首次：按需探活，种子 / 存量 active 版本以此为基线（TOFU）
    cv = await state.catalog.active_connector("mcp-ticket")
    assert cv is not None and cv.activated_manifest_hash == cv.manifest_hash is not None
    up.manifest.append({"name": "ticket.export", "risk_level": "read"})  # 连接器换了 manifest
    await connectors.probe_all()
    r = c.post("/invoke", json=body)
    assert r.status_code == 409 and r.json()["code"] == "manifest_drift"
    assert c.post("/mcp/servers/mcp-ticket/versions/1/activate").status_code == 200  # 重新激活即新基线
    assert c.post("/invoke", json=body).status_code == 200
    ghost = Capability(id="cap.ticket.ghost", name="ticket.ghost", description="d", system="ticket", connector="mcp-ticket")
    await state.catalog.upsert(ghost)
    r = c.post("/invoke", json=body | {"tool": "ticket.ghost"})
    assert r.status_code == 409 and r.json()["code"] == "tool_not_in_manifest"
    up.connector_down = True
    await state.catalog.put_connector(ConnectorVersion(name="mcp-ticket", version="5", endpoint=CONNECTOR_URL, system="ticket"))
    await state.catalog.activate("mcp-ticket", "5")  # 从未探活成功的 active 版本：按需探活失败 → 503
    r = c.post("/invoke", json=body)
    assert r.status_code == 503 and r.json()["code"] == "connector_manifest_unavailable"
    assert all(req.url.path != "/tools/ticket.ghost" for req in up.connector)
