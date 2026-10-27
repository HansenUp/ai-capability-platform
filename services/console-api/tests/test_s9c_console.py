"""S9 WP-C：Console 节点鉴权（静态令牌 / ai-node-svc JWT）、指令 ack、Skill 推送、审批代理、评测回放编排。"""

from __future__ import annotations

import io
import json
import tarfile
import time
from datetime import UTC, datetime

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

NODE = {"Authorization": "Bearer test-node-token"}
TADMIN = {"X-Console-User": "root", "X-Console-Role": "TENANT_ADMIN"}
SHA = "a" * 64


# ---- 节点鉴权 ----
def test_node_endpoints_require_service_token(c):
    assert c.post("/nodes/register", json={"node_id": "x1"}).status_code == 401
    assert c.post("/nodes/register", json={"node_id": "x1"}, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert c.get("/nodes/x1/commands").status_code == 401
    assert c.post("/nodes/x1/commands/c/ack", json={"status": "ok"}).status_code == 401
    assert c.post("/nodes/x1/heartbeat", json={}, headers=TADMIN).status_code == 401  # 操作者身份头不能代替节点令牌


@pytest.fixture
def jwks_mode(monkeypatch):
    from console_api import node_auth

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key())) | {"kid": "k1", "use": "sig", "alg": "RS256"}
    node_auth.set_transport_for_tests(httpx.MockTransport(lambda r: httpx.Response(200, json={"keys": [jwk]})))
    monkeypatch.setenv("CONSOLE_NODE_JWKS_URL", "http://kc.test/certs")
    monkeypatch.setenv("CONSOLE_NODE_ISSUER", "http://kc.test/realms/ai-platform")

    def mint(azp="ai-node-svc", exp_in=300, kid="k1", iss="http://kc.test/realms/ai-platform"):
        claims = {"azp": azp, "iss": iss, "exp": int(time.time()) + exp_in, "sub": "service-account-ai-node-svc"}
        return {"Authorization": "Bearer " + jwt.encode(claims, key, algorithm="RS256", headers={"kid": kid})}

    yield mint
    node_auth.set_transport_for_tests(None)


def test_node_jwt_mode(c, jwks_mode):
    body = {"node_id": "j1"}
    assert c.post("/nodes/register", json=body, headers=jwks_mode()).status_code == 200
    assert c.post("/nodes/register", json=body, headers=NODE).status_code == 401  # JWKS 模式下静态令牌无效
    assert c.post("/nodes/register", json=body, headers=jwks_mode(azp="ai-runtime-svc")).status_code == 403
    assert c.post("/nodes/register", json=body, headers=jwks_mode(exp_in=-120)).status_code == 401
    assert c.post("/nodes/register", json=body, headers=jwks_mode(kid="nope")).status_code == 401
    assert c.post("/nodes/register", json=body, headers=jwks_mode(iss="http://evil")).status_code == 401


def test_console_prod_requires_node_jwks(monkeypatch):
    from console_api import node_auth

    monkeypatch.delenv("CONSOLE_NODE_JWKS_URL", raising=False)
    assert len(node_auth.production_problems()) == 2  # 缺 JWKS + 配了静态令牌
    monkeypatch.setenv("CONSOLE_NODE_JWKS_URL", "http://kc/certs")
    monkeypatch.delenv("CONSOLE_NODE_TOKEN")
    assert node_auth.production_problems() == []


# ---- 指令 ack 与推送 ----
def test_ack_semantics(c):
    assert c.post("/nodes/register", json={"node_id": "a1"}, headers=NODE).status_code == 200
    cid = c.post("/nodes/a1/commands", json={"type": "reload_config"}, headers=TADMIN).json()["id"]
    assert c.post("/nodes/a1/commands/unknown/ack", json={"status": "ok"}, headers=NODE).status_code == 404
    assert c.post(f"/nodes/other/commands/{cid}/ack", json={"status": "ok"}, headers=NODE).status_code == 404  # 不能回执别的节点的指令
    assert c.post(f"/nodes/a1/commands/{cid}/ack", json={"status": "bogus"}, headers=NODE).status_code == 422
    r = c.post(f"/nodes/a1/commands/{cid}/ack", json={"status": "unsupported", "detail": {"type": "reload_config"}}, headers=NODE)
    assert r.status_code == 200 and r.json()["status"] == "unsupported"
    again = c.post(f"/nodes/a1/commands/{cid}/ack", json={"status": "ok"}, headers=NODE).json()
    assert again["status"] == "unsupported"  # 幂等：保留首次回执
    st = c.get(f"/nodes/a1/commands/{cid}", headers=TADMIN).json()
    assert st["status"] == "unsupported" and st["acked_at"]
    assert c.get(f"/nodes/a1/commands/{cid}", headers=TADMIN | {"X-Console-Tenant": "t2"}).status_code == 404


def test_skills_push(c):
    c.post("/nodes/register", json={"node_id": "p1", "labels": ["skills-a"]}, headers=NODE)
    c.post("/nodes/register", json={"node_id": "p2", "labels": ["skills-b"]}, headers=NODE)
    skills = [{"name": "ticket-summary", "version": "1.0.0", "manifest_sha256": SHA}]
    r = c.post("/skills/push", json={"skills": skills, "label": "skills-a"}, headers=TADMIN)
    assert r.status_code == 202 and [x["node_id"] for x in r.json()["commands"]] == ["p1"]
    cmds = c.get("/nodes/p1/commands", headers=NODE).json()
    assert cmds[-1]["type"] == "reload_skills" and cmds[-1]["args"] == {"skills": skills}
    assert c.post("/skills/push", json={"skills": skills, "label": "nobody"}, headers=TADMIN).status_code == 409
    bad = [{"name": "../x", "version": "1", "manifest_sha256": SHA}]
    assert c.post("/skills/push", json={"skills": bad, "nodes": ["p1"]}, headers=TADMIN).status_code == 422
    admin = {"X-Console-User": "alice", "X-Console-Role": "ADMIN"}
    assert c.post("/skills/push", json={"skills": skills, "nodes": ["p1"]}, headers=admin).status_code == 403


# ---- 审批代理 ----
@pytest.fixture
def gateway():
    from console_api import gateway_client

    seen: list[httpx.Request] = []
    routes: dict[tuple[str, str], httpx.Response] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        key = (request.method, request.url.path)
        if key in routes:
            v = routes[key]
            return v() if callable(v) else v
        return httpx.Response(404, json={"detail": "not found"})

    gateway_client.set_transport_for_tests(httpx.MockTransport(handler))
    yield type("GW", (), {"seen": seen, "routes": routes})
    gateway_client.set_transport_for_tests(None)


def test_approve_proxy(c, gateway):
    path = "/skills/ticket-summary/versions/1.0.0/approve"
    gateway.routes[("POST", path)] = httpx.Response(200, json={"status": "listed"})
    hdr = {"X-Console-User": "bob", "X-Console-Role": "USER", "Authorization": "Bearer user-jwt-bob"}
    r = c.post(path, headers=hdr)
    assert r.status_code == 200 and r.json() == {"status": "listed"}
    assert gateway.seen[-1].headers["Authorization"] == "Bearer user-jwt-bob"  # 开发态：审批人令牌直传
    gateway.routes[("POST", path)] = httpx.Response(403, json={"detail": "self approval forbidden", "code": "FORBIDDEN"})
    r = c.post(path, headers=hdr)
    assert r.status_code == 403 and "self approval" in r.json()["detail"]
    assert c.post(path, headers={"X-Console-User": "bob", "X-Console-Role": "USER"}).status_code == 401
    # 审核页其余转发：列表 / 详情 / 吊销（X-AI-User-Token 优先）
    gateway.routes[("GET", "/skills")] = httpx.Response(200, json={"items": [{"name": "ticket-summary", "status": "evaluated"}]})
    r = c.get("/skills?status=evaluated", headers={"X-Console-Role": "USER", "X-AI-User-Token": "tok"})
    assert r.status_code == 200 and r.json()["items"][0]["name"] == "ticket-summary"
    assert gateway.seen[-1].url.params["status"] == "evaluated" and gateway.seen[-1].headers["Authorization"] == "Bearer tok"
    gateway.routes[("GET", "/skills/ticket-summary/versions/1.0.0")] = httpx.Response(200, json={"status": "listed", "tenant": "default"})
    assert c.get("/skills/ticket-summary/versions/1.0.0", headers=hdr).json() == {"status": "listed", "tenant": "default"}
    rv = "/skills/ticket-summary/versions/1.0.0/revoke"
    gateway.routes[("POST", rv)] = httpx.Response(200, json={"status": "revoked"})
    r = c.post(rv, json={"reason": "漏洞"}, headers=TADMIN | {"X-AI-User-Token": "admin-tok"})
    assert r.status_code == 200 and json.loads(gateway.seen[-1].content) == {"reason": "漏洞"}
    assert c.post(rv, json={"reason": "x"}, headers=hdr).status_code == 403  # 吊销需租户管理员
    assert c.post(rv, json={}, headers=TADMIN | {"X-AI-User-Token": "t"}).status_code == 422


# ---- 评测回放 ----
def _cases(n: int, *, private: bool = False) -> list[dict]:
    out = []
    for i in range(n):
        case = {"id": f"c{i}", "input": f"总结工单 T-{1000 + i}", "must_call": ["ticket.get"], "must_not_call": ["ticket.close_batch"]}
        case["contains"] = ["根因"] if i else ["不会出现的词"]  # 第 0 条断言失败
        if private and i == 3:
            case["data_class"] = "confidential"
        out.append(case)
    return out


class FakeLangfuse:
    def __init__(self) -> None:
        self.items: dict[str, dict] = {}
        self.run_items: list[dict] = []
        self.scores: dict[str, list[dict]] = {}
        self.traces: dict[str, dict] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        p = request.url.path
        body = json.loads(request.content) if request.content else {}
        if request.method == "POST":
            if p == "/api/public/dataset-items":
                self.items[body["id"]] = body
            elif p == "/api/public/scores":
                self.scores.setdefault(body["traceId"], []).append({"name": body["name"], "value": body["value"]})
            elif p == "/api/public/dataset-run-items":
                self.run_items.append(body | {"createdAt": datetime.now(UTC).isoformat()})
            return httpx.Response(200, json={})
        if p.startswith("/api/public/traces/"):
            tid = p.rsplit("/", 1)[1]
            return httpx.Response(200, json=self.traces[tid] | {"scores": self.scores.get(tid, [])})
        if "/runs/" in p:
            items = [
                {"datasetItemId": r["datasetItemId"], "traceId": r.get("traceId"), "createdAt": r["createdAt"]} for r in self.run_items
            ]
            return httpx.Response(200, json={"metadata": self.run_items[0]["metadata"], "datasetRunItems": items})
        return httpx.Response(404)


@pytest.fixture
def eval_env(gateway, runtime, monkeypatch):
    from console_api import runtime_client, skill_eval

    lf = FakeLangfuse()
    asked: list[dict] = []
    published: list[tuple[str, dict]] = []

    def rt(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        if request.url.path == "/v1/messages":
            asked.append(body)
            n = len(asked)
            tid = f"trace-{n}"
            lf.traces[tid] = {
                "timestamp": datetime.now(UTC).isoformat(),
                "metadata": {
                    "attributes": {"ai.agent_id": body["agent_id"], "ai.agent_version": body["agent_version"], "ai.tenant_id": "default"}
                },
            }
            events = [
                {"type": "tool_use", "call_id": "x", "tool_name": "ticket.get", "input": {}},
                {"type": "done", "trace_id": tid, "stop_reason": "end_turn"},
            ]
            return httpx.Response(200, json={"content": "根因：配置错误", "run_id": f"r{n}", "events": events})
        published.append((request.url.path, body))
        return httpx.Response(200, json={"ok": True})

    runtime_client.set_transport_for_tests(httpx.MockTransport(rt))
    skill_eval.set_langfuse_transport_for_tests(httpx.MockTransport(lf.handler))
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    info = {
        "name": "ticket-summary",
        "version": "1.0.0",
        "tenant": "default",
        "status": "scanned",
        "manifest": {"capabilities": ["cap.ticket.search", "cap.ticket.get"]},
    }
    gateway.routes[("GET", "/skills/ticket-summary/versions/1.0.0")] = lambda: httpx.Response(200, json=info)
    gateway.routes[("POST", "/skills/ticket-summary/versions/1.0.0/eval-result")] = httpx.Response(200, json={"status": "evaluated"})
    yield type("E", (), {"lf": lf, "asked": asked, "published": published, "gw": gateway, "info": info})
    runtime_client.set_transport_for_tests(httpx.MockTransport(runtime.handler))
    skill_eval.set_langfuse_transport_for_tests(None)


def _package(cases: list[dict], eval_json: dict | None = None) -> bytes:
    files = {
        "SKILL.md": b"---\nname: ticket-summary\ndescription: d\n---\nSKILL-BODY",
        "evals/cases.jsonl": "\n".join(json.dumps(x) for x in cases).encode(),
    }
    if eval_json is not None:
        files["evals/eval.json"] = json.dumps(eval_json).encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for p, d in files.items():
            ti = tarfile.TarInfo(p)
            ti.size = len(d)
            tar.addfile(ti, io.BytesIO(d))
    return buf.getvalue()


def test_skill_eval_replay_langfuse(c, eval_env):
    eval_env.gw.routes[("GET", "/skills/ticket-summary/versions/1.0.0/package")] = httpx.Response(200, content=_package(_cases(10)))
    r = c.post("/skills/ticket-summary/versions/1.0.0/eval", json={}, headers=TADMIN)
    assert r.status_code == 200, r.text
    s = r.json()
    assert s["total"] == 10 and s["passed"] == 9 and s["pass_rate"] == 0.9 and s["gate_passed"] and s["threshold"] == 0.8
    assert s["run_ref"].startswith("langfuse:skill-ticket-summary/ticket-summary@1.0.0-")
    # 预发布临时 Agent：0% 灰度 + 白名单，工具 = 包声明能力（去 cap. 前缀），SKILL.md 内联
    snap = next(b for p, b in eval_env.published if p == "/admin/agents")
    assert snap["agent_id"] == "__skilleval-ticket-summary" and snap["config"]["tools"] == ["ticket.get", "ticket.search"]
    assert "SKILL-BODY" in snap["system_prompt"]
    pub = next(b for p, b in eval_env.published if p.endswith("/publish"))
    assert pub["rollout_percent"] == 0 and pub["rollout_allowlist"] == ["human:skilleval"]
    assert all(a["agent_id"] == "__skilleval-ticket-summary" and a["stream"] is False for a in eval_env.asked)
    # 写回 Langfuse：每条一个 eval_pass 评分；网关收到 run_ref / pass_rate / items
    assert sum(len(v) for v in eval_env.lf.scores.values()) == 10
    sent = json.loads(eval_env.gw.seen[-1].content)
    assert eval_env.gw.seen[-1].url.path.endswith("/eval-result") and sent["pass_rate"] == 0.9 and len(sent["items"]) == 10
    assert sent["run_ref"] == s["run_ref"]


def test_skill_eval_threshold_and_local_mode(c, eval_env, monkeypatch):
    eval_env.info["skill_md"] = "SKILL-BODY"
    eval_env.info["eval_cases"] = _cases(10)
    eval_env.info["eval_min_pass_rate"] = 0.95  # 包内声明高于平台下限：取较大者
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY")
    r = c.post("/skills/ticket-summary/versions/1.0.0/eval", json={}, headers=TADMIN).json()
    assert r["run_ref"].startswith("local:") and r["pass_rate"] == 0.9 and r["threshold"] == 0.95 and not r["gate_passed"]


def test_skill_eval_refusals(c, eval_env):
    url = "/skills/ticket-summary/versions/1.0.0/eval"
    eval_env.info |= {"skill_md": "SKILL-BODY", "eval_cases": []}  # 版本信息直接带材料：不下载包
    assert c.post(url, json={"cases": _cases(5)}, headers=TADMIN).status_code == 409  # 条数不足
    r = c.post(url, json={"cases": _cases(10, private=True)}, headers=TADMIN)
    assert r.status_code == 409 and "public" in r.json()["detail"]
    eval_env.info["status"] = "listed"
    assert c.post(url, json={"cases": _cases(10)}, headers=TADMIN).status_code == 409
    eval_env.info["status"], eval_env.info["tenant"] = "scanned", "t2"
    assert c.post(url, json={"cases": _cases(10)}, headers=TADMIN).status_code == 404
    assert c.post(url, json={}, headers={"X-Console-User": "a", "X-Console-Role": "ADMIN"}).status_code == 403


def test_judge_rules():
    from console_api.skill_eval import Case, judge

    case = Case("x", "q", must_call=["a"], must_not_call=["b"], contains=["根因|原因", "处理"])
    assert judge(case, "原因与处理", ["a"]) == []
    assert len(judge(case, "无", ["b"])) == 4


@pytest.mark.parametrize("stop,error", [("error", False), ("cancelled", False), ("awaiting", False), (None, False), ("end_turn", True)])
async def test_skill_eval_failed_runtime_is_not_a_passing_case(monkeypatch, stop, error):
    from unittest.mock import AsyncMock

    from console_api import skill_eval

    events = [{"type": "done", "stop_reason": stop, "trace_id": "trace"}]
    if error:
        events.insert(0, {"type": "error", "code": "DATA_CLASS_MODEL_BLOCKED"})
    monkeypatch.setattr(skill_eval.runtime_client, "call", AsyncMock(return_value={"content": "", "run_id": "run", "events": events}))
    result = await skill_eval._ask("agent", "version", skill_eval.Case("case", "question"), "default")
    assert result.get("error")


@pytest.mark.parametrize("level", ["internal", "confidential"])
def test_skill_eval_preserves_manifest_class_in_temporary_agent(c, eval_env, level):
    eval_env.info |= {"skill_md": "SKILL-BODY", "eval_cases": _cases(10)}
    eval_env.info["manifest"]["data_class"] = level
    response = c.post("/skills/ticket-summary/versions/1.0.0/eval", json={}, headers=TADMIN)
    assert response.status_code == 200, response.text
    snapshot = next(body for path, body in eval_env.published if path == "/admin/agents")
    assert snapshot["config"]["data_class"] == level


def test_skill_eval_rejects_replaced_package_cases(c, eval_env):
    eval_env.info |= {"skill_md": "SKILL-BODY", "eval_cases": _cases(10)}
    weak = [{"id": case["id"], "input": "通过"} for case in _cases(10)]
    response = c.post("/skills/ticket-summary/versions/1.0.0/eval", json={"cases": weak}, headers=TADMIN)
    assert response.status_code == 409 and "immutable" in response.json()["detail"]
    assert not eval_env.asked
    assert not any(request.url.path.endswith("/eval-result") for request in eval_env.gw.seen)


@pytest.mark.parametrize("identity", ["run_version", "trace_tenant", "trace_version"])
def test_skill_eval_rejects_mismatched_langfuse_identity(c, eval_env, identity):
    from console_api import skill_eval

    eval_env.info |= {"skill_md": "SKILL-BODY", "eval_cases": _cases(10)}

    def handler(request):
        response = eval_env.lf.handler(request)
        if request.method == "GET" and ((identity == "run_version" and "/runs/" in request.url.path) or "/traces/" in request.url.path):
            body = response.json()
            if identity == "run_version" and "/runs/" in request.url.path:
                body["metadata"]["agent_version"] = "another-version"
            elif identity.startswith("trace_") and "/traces/" in request.url.path:
                key = "ai.tenant_id" if identity == "trace_tenant" else "ai.agent_version"
                body["metadata"]["attributes"][key] = "another"
            return httpx.Response(200, json=body)
        return response

    skill_eval.set_langfuse_transport_for_tests(httpx.MockTransport(handler))
    response = c.post("/skills/ticket-summary/versions/1.0.0/eval", json={}, headers=TADMIN)
    assert response.status_code == 502 and "identity" in response.json()["detail"]
    assert not any(request.url.path.endswith("/eval-result") for request in eval_env.gw.seen)


@pytest.mark.parametrize("unavailable", ["items", "scores", "trace"])
def test_skill_eval_waits_for_langfuse_ingestion(c, eval_env, unavailable):
    from console_api import skill_eval

    eval_env.info |= {"skill_md": "SKILL-BODY", "eval_cases": _cases(10)}
    delayed = []

    def handler(request):
        response = eval_env.lf.handler(request)
        path = request.url.path
        if (
            request.method == "GET"
            and not delayed
            and ((unavailable == "items" and "/runs/" in path) or (unavailable != "items" and "/traces/" in path))
        ):
            delayed.append(path)
            if unavailable == "trace":
                return httpx.Response(404)
            body = response.json()
            body["datasetRunItems" if unavailable == "items" else "scores"] = []
            return httpx.Response(200, json=body)
        return response

    skill_eval.set_langfuse_transport_for_tests(httpx.MockTransport(handler))
    response = c.post("/skills/ticket-summary/versions/1.0.0/eval", json={}, headers=TADMIN)
    assert response.status_code == 200 and response.json()["passed"] == 9
    assert delayed


def test_skill_eval_timeout_does_not_submit_empty_evaluation(c, eval_env, monkeypatch):
    from console_api import skill_eval

    eval_env.info |= {"skill_md": "SKILL-BODY", "eval_cases": _cases(10)}
    monkeypatch.setenv("SKILL_EVAL_INGEST_TIMEOUT_S", "0")

    def handler(request):
        response = eval_env.lf.handler(request)
        if request.method == "GET" and "/runs/" in request.url.path:
            return httpx.Response(200, json=response.json() | {"datasetRunItems": []})
        return response

    skill_eval.set_langfuse_transport_for_tests(httpx.MockTransport(handler))
    response = c.post("/skills/ticket-summary/versions/1.0.0/eval", json={}, headers=TADMIN)
    assert response.status_code == 502 and "timed out" in response.json()["detail"]
    assert not any(request.url.path.endswith("/eval-result") for request in eval_env.gw.seen)


async def test_skill_eval_production_never_falls_back_to_local(monkeypatch):
    from console_api import skill_eval

    monkeypatch.setenv("CONSOLE_ENV", "prod")
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    with pytest.raises(skill_eval.SkillEvalError, match="requires Langfuse"):
        await skill_eval.run("ticket-summary", "1.0.0", tenant="default")


def test_skill_revoke_checks_tenant_before_admin_forward(c, gateway):
    path = "/skills/ticket-summary/versions/1.0.0"
    gateway.routes[("GET", path)] = httpx.Response(200, json={"tenant": "other", "status": "listed"})
    response = c.post(path + "/revoke", json={"reason": "unsafe"}, headers=TADMIN | {"X-AI-User-Token": "admin-token"})
    assert response.status_code == 404
    assert not any(request.method == "POST" for request in gateway.seen)
