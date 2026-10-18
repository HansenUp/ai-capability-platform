"""verify-s8 五条链路（方案 §6）的网关级用例骨架。需要全栈，默认跳过：``S8_E2E_STACK=1`` 时运行。

前置（WP-D 第二阶段由 dev_stack / verify_s8_e2e / 夜间 CI 准备）：Keycloak（含 ``ai-runtime-svc`` 客户端与
mcp-knowledge / mcp-crm / mcp-mail 受众）、能力网关（act 签名、Cedar 策略）、策略服务 + Cedar 引擎、mcp-ticket（生产态、
``MCP_TICKET_ACT_JWKS_URL`` 指向网关 JWKS）与三个 mock（生产态）。连接器在网关登记的名称须等于受众名（act 断言 aud=连接器名）。

本文件只断言网关与连接器层面的契约（§4 冻结契约 2–4）；经 Runtime 的审批卡片、拒绝 / 批准流程由 verify_s8_e2e 驱动，
这里用 ``approved`` 字段模拟 Runtime 闸口已审批，用发件箱计数断言"拒绝 0、批准 1"。

环境变量：``GATEWAY_URL``（缺省 http://127.0.0.1:8082）、``POLICY_URL``（缺省 http://127.0.0.1:8083）、
``MCP_KNOWLEDGE_URL`` / ``MCP_CRM_URL`` / ``MCP_MAIL_URL``（缺省 127.0.0.1:8092–8094）；令牌经 ``scripts/devkit.py`` 获取。
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

from mcp_template.testing import ALICE

pytestmark = pytest.mark.skipif(
    os.environ.get("S8_E2E_STACK") != "1",
    reason="需全栈（Keycloak、网关、策略 + Cedar、mcp-ticket 与 mock 连接器）；S8_E2E_STACK=1 时运行（verify-s8 / 夜间 CI）",
)

ROOT = Path(__file__).resolve().parents[2]
GATEWAY = os.environ.get("GATEWAY_URL", "http://127.0.0.1:8082")
POLICY = os.environ.get("POLICY_URL", "http://127.0.0.1:8083")
MOCK = {
    "knowledge": os.environ.get("MCP_KNOWLEDGE_URL", "http://127.0.0.1:8092"),
    "crm": os.environ.get("MCP_CRM_URL", "http://127.0.0.1:8093"),
    "mail": os.environ.get("MCP_MAIL_URL", "http://127.0.0.1:8094"),
}
AGENT, VERSION = "demo", "1"


def _devkit() -> Any:
    sys.path.insert(0, str(ROOT / "scripts"))
    import devkit  # 仅全栈模式下导入（需要本机 Keycloak）

    return devkit


@pytest.fixture(scope="module")
def tokens() -> dict[str, str]:
    """服务令牌（ai-runtime-svc，client credentials）与终端用户令牌。"""
    dk = _devkit()
    return {"svc": dk.client_token("ai-runtime-svc"), "alice": dk.user_token("alice"), "bob": dk.user_token("bob")}


@pytest.fixture(autouse=True)
def reset_mocks() -> None:
    for url in MOCK.values():
        httpx.post(f"{url}/_mock/reset", timeout=5).raise_for_status()


def invoke(tokens: dict[str, str], user: str, tool: str, payload: dict[str, Any], **kw: Any) -> httpx.Response:
    """契约 3：服务令牌放 Authorization，终端用户令牌放 X-AI-User-Token。"""
    body = {"tool": tool, "input": payload, "agent_id": AGENT, "agent_version": VERSION, "run_id": f"run-{uuid.uuid4().hex[:12]}"} | kw
    headers = {"Authorization": f"Bearer {tokens['svc']}", "X-AI-User-Token": tokens[user]}
    return httpx.post(f"{GATEWAY}/invoke", json=body, headers=headers, timeout=30)


def _ok(r: httpx.Response) -> Any:
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["ok"], out
    return out["output"]


def _last_call(name: str) -> dict[str, Any]:
    calls = httpx.get(f"{MOCK[name]}/_mock/calls", timeout=5).json()
    assert calls, f"{name} received no call"
    last: dict[str, Any] = calls[-1]
    return last


def _outbox() -> int:
    count: int = httpx.get(f"{MOCK['mail']}/_mock/outbox", timeout=5).json()["count"]
    return count


def test_chain1_readonly_obo_and_act(tokens: dict[str, str]) -> None:
    """① 只读：mcp-ticket（生产态）只在收到 aud=mcp-ticket 的 OBO 且 act 断言有效时返回 200；判定引擎为 cedar。"""
    out = _ok(invoke(tokens, "alice", "ticket.search", {"limit": 5}))
    assert out["total"] >= 1
    principal = {"sub": ALICE, "tenant": "default", "roles": ["sales"]}
    resource = {"id": "cap.ticket.search", "tenant": "default", "system": "ticket", "risk_level": "read", "data_class": "internal"}
    context = {"approved": False, "taint": [], "agent": {"id": AGENT, "version": VERSION, "tenant": "default"}}
    request = {"principal": principal, "action": "invoke", "resource": resource, "context": context}
    r = httpx.post(f"{POLICY}/authz/check-batch", json={"requests": [request]}, timeout=10)
    assert r.status_code == 200 and r.json()["engine"] == "cedar"


def test_chain2_high_risk_write_exactly_once(tokens: dict[str, str]) -> None:
    """② 高风险写：预演 → （Runtime 审批）→ 恰好执行一次；用户令牌直连网关 401；最后用补偿工具复原。"""
    ids = ["T-1002"]
    key = f"s8-e2e-close-{uuid.uuid4().hex[:8]}"
    # 复原前一次中断运行可能留下的"已关闭"状态（mcp-ticket 的状态覆盖持久化在 SQLite），保证预演有影响面
    _ok(invoke(tokens, "alice", "ticket.reopen_batch", {"ids": ids}, idempotency_key=f"{key}-reset", approved=True))
    preview = _ok(invoke(tokens, "alice", "ticket.close_batch", {"ids": ids}, dry_run=True))
    assert preview["dry_run"] is True and preview["impact_preview"]["ids"] == ids
    first = _ok(invoke(tokens, "alice", "ticket.close_batch", {"ids": ids}, idempotency_key=key, approved=True))
    again = _ok(invoke(tokens, "alice", "ticket.close_batch", {"ids": ids}, idempotency_key=key, approved=True))
    assert first["deduplicated"] is False and again["deduplicated"] is True and first["by"] == f"agent:{AGENT}@{VERSION}"
    direct = httpx.post(
        f"{GATEWAY}/invoke",
        json={"tool": "ticket.search", "input": {}, "agent_id": AGENT, "agent_version": VERSION},
        headers={"Authorization": f"Bearer {tokens['alice']}"},
        timeout=10,
    )
    assert direct.status_code == 401
    undo = _ok(invoke(tokens, "alice", "ticket.reopen_batch", {"ids": ids}, idempotency_key=f"{key}-undo", approved=True))
    assert undo["reopened"] == ids


def test_chain3_knowledge_hybrid_hit_acl_and_taint(tokens: dict[str, str]) -> None:
    """③ 知识检索：混合检索发现 knowledge.search → 调用按 ACL 过滤；带 confidential 污点时外发能力不再放行。"""
    headers = {"Authorization": f"Bearer {tokens['svc']}", "X-AI-User-Token": tokens["alice"]}
    found = httpx.post(
        f"{GATEWAY}/capabilities/search", json={"query": "查企业知识库文档", "agent_id": AGENT, "agent_version": VERSION}, headers=headers
    ).json()
    assert "knowledge.search" in {i["name"] for i in found["items"]}
    alice = _ok(invoke(tokens, "alice", "knowledge.search", {"query": "折扣审批权限"}))
    bob = _ok(invoke(tokens, "bob", "knowledge.search", {"query": "折扣审批权限"}))
    assert "K-004" in {i["id"] for i in alice["items"]} and "K-004" not in {i["id"] for i in bob["items"]}
    tainted = httpx.post(
        f"{GATEWAY}/capabilities/search",
        json={"query": "发送邮件", "agent_id": AGENT, "agent_version": VERSION, "taint": ["confidential"]},
        headers=headers,
    ).json()
    assert all(i["decision"] != "allow" for i in tainted["items"] if i["name"] == "mail.send")


def test_chain4_cross_system_distinct_audiences(tokens: dict[str, str]) -> None:
    """④ 跨系统：同一用户、同一 Run 调 crm 与 knowledge，两个连接器各自收到受众为自己的 OBO 与已验签 act 断言。"""
    run_id = f"run-{uuid.uuid4().hex[:12]}"
    _ok(invoke(tokens, "alice", "crm.opportunity.search", {}, run_id=run_id))
    _ok(invoke(tokens, "alice", "knowledge.search", {"query": "GW-200"}, run_id=run_id))
    crm, kb = _last_call("crm"), _last_call("knowledge")
    assert (crm["audience"], kb["audience"]) == ("mcp-crm", "mcp-knowledge")
    assert crm["sub"] == kb["sub"] and crm["act_verified"] and kb["act_verified"]
    assert crm["act"] == kb["act"] == f"agent:{AGENT}@{VERSION}" and crm["run_id"] == kb["run_id"] == run_id


def test_chain5_outbound_after_confidential_read(tokens: dict[str, str]) -> None:
    """⑤ 外发：读过 crm（confidential 污点）后 mail.send 判定 require_approval；拒绝 → 发件箱 0，批准 → 恰好 1。"""
    _ok(invoke(tokens, "alice", "crm.opportunity.search", {}))
    principal = {"sub": ALICE, "tenant": "default", "roles": ["sales"]}
    resource = {"id": "cap.mail.send", "tenant": "default", "system": "outbound", "risk_level": "write_low", "data_class": "internal"}
    context = {"approved": False, "taint": ["confidential"], "agent": {"id": AGENT, "version": VERSION, "tenant": "default"}}
    request = {"principal": principal, "action": "invoke", "resource": resource, "context": context}
    r = httpx.post(f"{POLICY}/authz/check-batch", json={"requests": [request]}, timeout=10)
    assert r.json()["results"][0]["decision"] == "require_approval"
    msg = {"to": ["customer@client.example"], "subject": "商机跟进", "body": "附上报价。"}
    assert _ok(invoke(tokens, "alice", "mail.send", msg, dry_run=True, taint=["confidential"]))["dry_run"] is True
    assert _outbox() == 0  # 审批被拒：Runtime 不发出执行调用
    key = f"s8-e2e-mail-{uuid.uuid4().hex[:8]}"
    _ok(invoke(tokens, "alice", "mail.send", msg, idempotency_key=key, approved=True, taint=["confidential"]))
    _ok(invoke(tokens, "alice", "mail.send", msg, idempotency_key=key, approved=True, taint=["confidential"]))
    assert _outbox() == 1
