"""S9 安全用例的全栈部分（方案 §5 verify-s9 ②③⑤⑫ 与 9.9）：需要全栈，默认跳过，``S9_SECURITY_STACK=1`` 时运行。

前置（联调阶段由 dev_stack / verify_s9_e2e / 夜间 CI 准备）：Keycloak（含 ``ai-node-svc`` 客户端、``skill-reviewer`` 角色，
carol / frank 为审批人、alice 无审批角色）、能力网关（Skill 商店、扫描、签名，``GATEWAY_ADMIN_TOKEN``）、策略服务 + Cedar、
Console API（节点接口校验 ai-node-svc 服务令牌）。

覆盖：五个坏包经网关上传按规则 id 进入 scan_failed；自批 403、无审批角色 403、他人审批后 listed；声明能力 ⊄ Agent 工具时
resolve 409；吊销即时进入吊销列表且 resolve 409；外发污点（confidential 后调用 outbound 能力）不得直接放行；
Console 节点接口无令牌 / 错误受众令牌 401。

联调对齐点（契约 ② 未写死的细节，集中在本文件顶部常量与 ``upload``）：上传需管理令牌（``X-Gateway-Admin-Token``）+ 受信服务令牌
（``Authorization: Bearer``，ai-runtime-svc）+ 提交人用户令牌（``X-AI-User-Token``）；评测结果 items 为 ``{id, passed}``；吊销
请求体 ``{reason}``（管理令牌）；审批为服务令牌 + 审批人用户令牌。坏包与对照组按随机后缀改名后上传，避免"版本不可变"导致重跑冲突。

环境变量：``GATEWAY_URL``（缺省 http://127.0.0.1:8082）、``POLICY_URL``（缺省 http://127.0.0.1:8083）、
``CONSOLE_URL``（缺省 http://127.0.0.1:8081）、``GATEWAY_ADMIN_TOKEN``（缺省 dev-gateway-admin）；令牌经 ``scripts/devkit.py`` 获取。
"""

from __future__ import annotations

import io
import os
import sys
import tarfile
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from s9sec import BAD_SKILLS, FIXTURE_SKILLS, copy_tree

pytestmark = pytest.mark.skipif(
    os.environ.get("S9_SECURITY_STACK") != "1",
    reason="需全栈（Keycloak、网关 Skill 商店、策略 + Cedar、Console）；S9_SECURITY_STACK=1 时运行（verify-s9 / 夜间 CI）",
)

ROOT = Path(__file__).resolve().parents[2]
GATEWAY = os.environ.get("GATEWAY_URL", "http://127.0.0.1:8082")
POLICY = os.environ.get("POLICY_URL", "http://127.0.0.1:8083")
CONSOLE = os.environ.get("CONSOLE_URL", "http://127.0.0.1:8081")
ADMIN = {"X-Gateway-Admin-Token": os.environ.get("GATEWAY_ADMIN_TOKEN", "dev-gateway-admin")}
RUN = uuid.uuid4().hex[:6]


def _devkit() -> Any:
    sys.path.insert(0, str(ROOT / "scripts"))
    import devkit  # 仅全栈模式下导入（需要本机 Keycloak）

    return devkit


@pytest.fixture(scope="module")
def tokens() -> dict[str, str]:
    dk = _devkit()
    return {
        "svc": dk.client_token("ai-runtime-svc"),
        "console": dk.client_token("console-api"),
        "node": dk.client_token("ai-node-svc"),
        **{u: dk.user_token(u) for u in ("alice", "carol", "frank")},
    }


def renamed_copy(src: Path, dest_root: Path, name: str) -> Path:
    """复制夹具并把 frontmatter 的 name 与目录名改为 name（随机后缀，便于重跑）。"""
    dest = dest_root / name
    copy_tree(src, dest)
    md = dest / "SKILL.md"
    md.write_text(md.read_text(encoding="utf-8").replace(f"\nname: {src.name}\n", f"\nname: {name}\n", 1), encoding="utf-8")
    return dest


def tgz(src: Path) -> bytes:
    """源目录 → 未签名 tgz（以 python 读取内容，兼容透明加密；符号链接按原样打入，由网关拒绝）。"""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for p in sorted(src.rglob("*")):
            rel = p.relative_to(src).as_posix()
            info = tarfile.TarInfo(rel)
            if p.is_symlink():
                info.type, info.linkname = tarfile.SYMTYPE, os.readlink(p)
                tar.addfile(info)
            elif p.is_file():
                data = p.read_bytes()
                info.size, info.mode = len(data), 0o644
                tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def upload(src: Path, tokens: dict[str, str], submitter: str = "frank") -> dict[str, Any]:
    headers = dict(ADMIN) | {"Authorization": f"Bearer {tokens['svc']}", "X-AI-User-Token": tokens[submitter]}
    r = httpx.post(f"{GATEWAY}/skills", files={"package": (f"{src.name}.tgz", tgz(src), "application/gzip")}, headers=headers, timeout=60)
    assert r.status_code == 201, r.text
    out: dict[str, Any] = r.json()
    return out


def status_of(tokens: dict[str, str], name: str, version: str) -> dict[str, Any]:
    r = httpx.get(f"{GATEWAY}/skills/{name}/versions/{version}", headers={"Authorization": f"Bearer {tokens['svc']}"}, timeout=10)
    assert r.status_code == 200, r.text
    out: dict[str, Any] = r.json()
    return out


def approve(tokens: dict[str, str], who: str, name: str, version: str) -> httpx.Response:
    headers = {"Authorization": f"Bearer {tokens['svc']}", "X-AI-User-Token": tokens[who]}
    return httpx.post(f"{GATEWAY}/skills/{name}/versions/{version}/approve", headers=headers, json={}, timeout=30)


def resolve(tokens: dict[str, str], name: str, version: str, tools: list[str]) -> httpx.Response:
    body = {
        "agent_id": f"s9sec-{RUN}",
        "tenant": "default",
        "skills": [{"name": name, "version": version}],
        "tools": tools,
        "discovery_scope": [],
    }
    return httpx.post(f"{GATEWAY}/skills/resolve", json=body, headers={"Authorization": f"Bearer {tokens['svc']}"}, timeout=10)


# ---- 坏包 ----
@pytest.mark.parametrize(("fixture", "rule"), sorted(BAD_SKILLS.items()))
def test_bad_package_scan_failed(tokens: dict[str, str], tmp_path: Path, fixture: str, rule: str) -> None:
    src = renamed_copy(FIXTURE_SKILLS / fixture, tmp_path, f"{fixture}-{RUN}")
    created = upload(src, tokens)
    st = status_of(tokens, created["name"], created["version"])
    assert st["status"] == "scan_failed", st
    ids = [str(f.get("rule") or f.get("rule_id")) for f in (st["scan_report"] or {}).get("findings", [])]
    assert any(i == rule or i.startswith((f"{rule}.", f"{rule}:", f"{rule}/")) for i in ids), f"{fixture} 应命中 {rule}，实际 {ids}"
    assert approve(tokens, "carol", created["name"], created["version"]).status_code in (403, 409)  # 扫描失败的版本不可审批


# ---- 上架：自批 / 无角色 / 他人审批 / 能力子集 / 吊销 ----
@pytest.fixture(scope="module")
def evaluated(tokens: dict[str, str], tmp_path_factory: pytest.TempPathFactory) -> tuple[str, str]:
    """frank 提交对照组 → 扫描通过 → Console 回写评测结果（10/10）→ evaluated。"""
    src = renamed_copy(FIXTURE_SKILLS / "ok-baseline", tmp_path_factory.mktemp("ok"), f"ok-baseline-{RUN}")
    created = upload(src, tokens)
    name, version = created["name"], created["version"]
    assert status_of(tokens, name, version)["status"] == "scanned"
    items = [{"id": f"c{i:02d}", "passed": True} for i in range(1, 11)]
    r = httpx.post(
        f"{GATEWAY}/skills/{name}/versions/{version}/eval-result",
        json={"run_ref": f"local:s9sec/{RUN}", "pass_rate": 1.0, "items": items, "tenant": "default"},
        headers={"Authorization": f"Bearer {tokens['console']}"},
        timeout=10,
    )
    assert r.status_code == 200, r.text
    assert status_of(tokens, name, version)["status"] == "evaluated"
    return name, version


def test_self_approval_forbidden(tokens: dict[str, str], evaluated: tuple[str, str]) -> None:
    """提交人 frank 本身是 skill-reviewer，仍不得审批自己的提交（Cedar forbid:self_approve_skill）。"""
    r = approve(tokens, "frank", *evaluated)
    assert r.status_code == 403, r.text
    assert status_of(tokens, *evaluated)["status"] == "evaluated"


def test_approval_requires_reviewer_role(tokens: dict[str, str], evaluated: tuple[str, str]) -> None:
    assert approve(tokens, "alice", *evaluated).status_code == 403


def test_capability_subset_and_listing(tokens: dict[str, str], evaluated: tuple[str, str]) -> None:
    """他人审批 → 签名 → listed；声明能力 ⊄ Agent 工具 ∪ 发现范围时 resolve 409，覆盖时 200 并给出 lock。"""
    r = approve(tokens, "carol", *evaluated)
    assert r.status_code == 200, r.text
    st = status_of(tokens, *evaluated)
    assert st["status"] == "listed" and st["manifest_sha256"], st
    denied = resolve(tokens, *evaluated, tools=["knowledge.search"])
    assert denied.status_code == 409, denied.text
    ok = resolve(tokens, *evaluated, tools=["ticket.search"])
    assert ok.status_code == 200, ok.text
    assert ok.json()["lock"] == [{"name": evaluated[0], "version": evaluated[1], "manifest_sha256": st["manifest_sha256"]}]


def test_revocation_is_immediate(tokens: dict[str, str], evaluated: tuple[str, str]) -> None:
    name, version = evaluated
    headers = ADMIN | {"Authorization": f"Bearer {tokens['svc']}", "X-AI-User-Token": tokens["carol"]}
    r = httpx.post(f"{GATEWAY}/skills/{name}/versions/{version}/revoke", json={"reason": "s9 security test"}, headers=headers, timeout=10)
    assert r.status_code == 200, r.text
    rev = httpx.get(f"{GATEWAY}/skills/revocations", headers={"Authorization": f"Bearer {tokens['svc']}"}, timeout=10).json()
    assert any(x["name"] == name and x["version"] == version for x in rev["revoked"]), rev
    assert resolve(tokens, name, version, tools=["ticket.search"]).status_code == 409


# ---- 外发污点 ----
def test_confidential_taint_blocks_outbound_without_approval() -> None:
    """读过 confidential 数据的 Run 调用 outbound 能力（mail.send）不得直接放行，决定性策略为 forbid:taint_outbound。"""
    principal = {"sub": "00000000-0000-4000-8000-0000000a11ce", "tenant": "default", "roles": ["sales"]}
    resource = {"id": "cap.mail.send", "tenant": "default", "system": "outbound", "risk_level": "write_low", "data_class": "internal"}
    agent = {"id": "demo", "version": "1", "tenant": "default"}
    req = {
        "principal": principal,
        "action": "invoke",
        "resource": resource,
        "context": {"approved": False, "taint": ["confidential"], "agent": agent},
    }
    r = httpx.post(f"{POLICY}/authz/check-batch", json={"requests": [req]}, timeout=10)
    assert r.status_code == 200, r.text
    res = r.json()["results"][0]
    assert res["decision"] != "allow" and "forbid:taint_outbound" in res["policy_ids"], res


# ---- 节点只出不进 ----
@pytest.mark.parametrize("kind", [None, "user", "svc"])
def test_console_node_endpoints_require_node_token(tokens: dict[str, str], kind: str | None) -> None:
    """无令牌、用户令牌、错误受众（aud=capability-gateway）的服务令牌访问 Console 节点接口一律 401。"""
    headers = {"Authorization": f"Bearer {tokens['alice'] if kind == 'user' else tokens['svc']}"} if kind else {}
    r = httpx.post(f"{CONSOLE}/nodes/register", json={"node_id": f"s9sec-{RUN}"}, headers=headers, timeout=10)
    assert r.status_code == 401, r.text


def test_console_node_endpoints_accept_node_token(tokens: dict[str, str]) -> None:
    r = httpx.post(
        f"{CONSOLE}/nodes/register", json={"node_id": f"s9sec-{RUN}"}, headers={"Authorization": f"Bearer {tokens['node']}"}, timeout=10
    )
    assert r.status_code == 200, r.text
