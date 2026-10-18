"""策略服务 × Cedar 引擎测试：两引擎一致性（同一 tests.json）、批量契约、require_approval 合成、
Cedar 不可达 deny、prod 拒绝 baseline。

真实引擎用例需要先编译 cedar-engine（`cargo build --release`，或用 CEDAR_ENGINE_BIN 指定二进制）；
未编译时这些用例跳过，设置 CEDAR_ENGINE_REQUIRED=1（CI）则改为失败。
"""

import json
import os
import socket
import subprocess
import sys
import time
from functools import partial
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from policy_service import main

POLICY_DIR = Path(__file__).parents[1]
CASES = json.loads((POLICY_DIR / "cedar" / "tests.json").read_text())


def _batch_item(case: dict, i: int) -> dict:
    """tests.json 用例 → 契约 1 的单条请求。"""
    p = case["principal"]
    roles = p.get("roles", [p["role"]] if "role" in p else [])
    return {
        "principal": {"sub": f"user-{i}", "tenant": p["tenant"], "roles": roles},
        "action": case.get("action", "invoke"),
        "resource": {"id": f"cap.case.{i}", **case["resource"]},
        "context": case["context"],
    }


ALL_ITEMS = [_batch_item(c, i) for i, c in enumerate(CASES)]


def _engine_binary() -> Path | None:
    env = os.environ.get("CEDAR_ENGINE_BIN")
    candidates = [Path(env)] if env else []
    candidates += [POLICY_DIR / "cedar-engine" / "target" / t / "cedar-engine" for t in ("release", "debug")]
    return next((c for c in candidates if c.is_file()), None)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def cedar_url():
    """启动真实 cedar-engine 子进程（随机端口），返回其 URL。"""
    binary = _engine_binary()
    if binary is None:
        if os.environ.get("CEDAR_ENGINE_REQUIRED") == "1":
            pytest.fail("cedar-engine binary not found; run cargo build --release in services/policy/cedar-engine")
        pytest.skip("cedar-engine binary not built (cargo build --release in services/policy/cedar-engine)")
    port = _free_port()
    env = {
        **os.environ,
        "CEDAR_LISTEN": f"127.0.0.1:{port}",
        "CEDAR_POLICY_FILE": str(POLICY_DIR / "cedar" / "policies.cedar"),
        "CEDAR_SCHEMA_FILE": str(POLICY_DIR / "cedar" / "schema.cedarschema"),
    }
    proc = subprocess.Popen([str(binary)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)  # noqa: S603
    url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{url}/healthz", timeout=0.5).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.1)
    else:
        proc.kill()
        pytest.fail(f"cedar-engine did not start: {proc.stderr.read().decode() if proc.stderr else ''}")
    yield url
    proc.terminate()
    proc.wait(timeout=5)


def _client(monkeypatch, **env) -> TestClient:
    monkeypatch.setattr(main, "settings", main.load_settings(env))
    return TestClient(main.app)


def _check_batch(client: TestClient, items: list[dict]) -> dict:
    r = client.post("/authz/check-batch", json={"requests": items})
    assert r.status_code == 200, r.text
    return r.json()


def _assert_matches_cases(body: dict) -> None:
    assert len(body["results"]) == len(CASES)
    for case, res in zip(CASES, body["results"], strict=True):
        assert res["decision"] == case["expect"], case["name"]
        if "expect_reason" in case:
            assert res["reason"] == case["expect_reason"], case["name"]
        assert res["decision_id"].startswith("dec_")


# ---- 两引擎一致性 ----
def test_baseline_engine_passes_shared_cases(monkeypatch):
    body = _check_batch(_client(monkeypatch, POLICY_ENGINE="baseline"), ALL_ITEMS)
    assert body["engine"] == "baseline" and body["policy_version"] == "baseline"
    _assert_matches_cases(body)


def test_cedar_engine_passes_shared_cases(monkeypatch, cedar_url):
    body = _check_batch(_client(monkeypatch, POLICY_ENGINE="cedar", CEDAR_URL=cedar_url), ALL_ITEMS)
    assert body["engine"] == "cedar" and len(body["policy_version"]) == 16
    assert httpx.get(f"{cedar_url}/healthz").json()["policy_version"] == body["policy_version"]
    _assert_matches_cases(body)


def test_engines_agree_on_every_case(monkeypatch, cedar_url):
    """同一批请求两引擎的决策、原因与决定性策略 id 完全一致。"""
    base = _check_batch(_client(monkeypatch, POLICY_ENGINE="baseline"), ALL_ITEMS)["results"]
    cedar = _check_batch(_client(monkeypatch, POLICY_ENGINE="cedar", CEDAR_URL=cedar_url), ALL_ITEMS)["results"]
    for case, b, c in zip(CASES, base, cedar, strict=True):
        assert (b["decision"], b["reason"], b["policy_ids"]) == (c["decision"], c["reason"], c["policy_ids"]), case["name"]


def test_legacy_check_on_cedar(monkeypatch, cedar_url):
    """单条 /authz/check 走 Cedar 时补 engine 字段，并合成 require_approval。"""
    client = _client(monkeypatch, POLICY_ENGINE="cedar", CEDAR_URL=cedar_url)
    r = client.post(
        "/authz/check",
        json={
            "subject": "human:1",
            "capability": "cap.crm.close_batch",
            "principal": {"role": "sales", "tenant": "t1"},
            "resource": {"system": "CRM", "risk_level": "write_high", "data_class": "internal", "tenant": "t1"},
            "context": {"approved": False, "taint": []},
        },
    ).json()
    assert (r["decision"], r["engine"], r["matched"]) == ("require_approval", "cedar", "permit:write_high_approved")


# ---- 批量契约与 require_approval 合成（模拟 Cedar 应答） ----
def _mock_cedar(monkeypatch, handler):
    """把 httpx.AsyncClient 的传输层替换为 MockTransport，记录 Cedar 收到的请求体。"""
    seen: list[dict] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return handler(request, seen[-1])

    monkeypatch.setattr(httpx, "AsyncClient", partial(httpx.AsyncClient, transport=httpx.MockTransport(wrapped)))
    return seen


def test_require_approval_synthesized_from_two_queries(monkeypatch):
    """未审批请求发两次查询（approved=false / true），false 为 deny 且 true 为 allow → require_approval；已审批请求只查一次。"""
    client = _client(monkeypatch, POLICY_ENGINE="cedar", CEDAR_URL="http://cedar.test")

    def handler(_req, body):
        results = [
            {"decision": "allow", "policy_ids": ["permit:write_high_approved"], "errors": []}
            if q["context"]["approved"]
            else {"decision": "deny", "policy_ids": [], "errors": []}
            for q in body["requests"]
        ]
        return httpx.Response(200, json={"results": results, "policy_version": "v-test"})

    seen = _mock_cedar(monkeypatch, handler)
    unapproved = _batch_item(CASES[2], 0)
    approved = {**unapproved, "context": {"approved": True, "taint": []}}
    body = _check_batch(client, [unapproved, approved])
    assert len(seen) == 1, "one round trip per batch"
    queries = seen[0]["requests"]
    assert [q["context"]["approved"] for q in queries] == [False, True, True]
    assert [r["decision"] for r in body["results"]] == ["require_approval", "allow"]
    assert body["results"][0]["reason"] == "permit:write_high_approved"
    assert body["policy_version"] == "v-test"


def test_require_approval_needs_second_allow(monkeypatch):
    """两次查询都 deny → deny（审批也无法放行的请求不得提示审批）。"""
    client = _client(monkeypatch, POLICY_ENGINE="cedar", CEDAR_URL="http://cedar.test")
    _mock_cedar(
        monkeypatch,
        lambda _r, b: httpx.Response(
            200,
            json={
                "results": [{"decision": "deny", "policy_ids": ["forbid:cross_tenant"], "errors": []} for _ in b["requests"]],
                "policy_version": "v",
            },
        ),
    )
    res = _check_batch(client, [_batch_item(CASES[1], 0)])["results"][0]
    assert (res["decision"], res["reason"], res["policy_ids"]) == ("deny", "forbid:cross_tenant", ["forbid:cross_tenant"])


def test_cedar_entity_mapping(monkeypatch):
    """实体映射：principal=User::sub（tenant、roles）、resource=Capability::id（目录属性）、context 含 agent。"""
    client = _client(monkeypatch, POLICY_ENGINE="cedar", CEDAR_URL="http://cedar.test")
    seen = _mock_cedar(
        monkeypatch,
        lambda _r, b: httpx.Response(
            200,
            json={"results": [{"decision": "allow", "policy_ids": ["p"], "errors": []} for _ in b["requests"]], "policy_version": "v"},
        ),
    )
    item = {
        "principal": {"sub": "alice", "tenant": "t1", "roles": ["sales", "sales"]},
        "action": "invoke",
        "resource": {"id": "cap.crm.read", "tenant": "t1", "system": "CRM", "risk_level": "read", "data_class": "internal"},
        "context": {"approved": True, "taint": ["internal", "confidential"], "agent": {"id": "a1", "version": "2", "tenant": "t1"}},
    }
    _check_batch(client, [item])
    q = seen[0]["requests"][0]
    assert q["principal"] == {"type": "User", "id": "alice"}
    assert q["action"] == {"type": "Action", "id": "invoke"}
    assert q["resource"] == {"type": "Capability", "id": "cap.crm.read"}
    assert q["context"] == {"approved": True, "taint": ["confidential", "internal"], "agent": {"id": "a1", "version": "2", "tenant": "t1"}}
    user, cap = q["entities"]
    assert user["attrs"] == {"tenant": "t1", "roles": ["sales"]}
    assert cap["attrs"] == {"system": "CRM", "risk_level": "read", "data_class": "internal", "tenant": "t1"}


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, json={"error": "boom"}),
        httpx.Response(413, json={"error": "batch too large", "results": []}),
        httpx.Response(200, json={"results": [], "policy_version": "v"}),  # 条数不符
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"results": [{"decision": "allow"}, "x"], "policy_version": "v"}),
    ],
    ids=["http500", "http413", "count-mismatch", "not-json", "malformed-item"],
)
def test_bad_cedar_response_denies(monkeypatch, response):
    client = _client(monkeypatch, POLICY_ENGINE="cedar", CEDAR_URL="http://cedar.test")
    _mock_cedar(monkeypatch, lambda _r, _b: response)
    body = _check_batch(client, ALL_ITEMS[:2])
    assert [r["decision"] for r in body["results"]] == ["deny", "deny"]
    assert {r["reason"] for r in body["results"]} == {"policy_unavailable"}


def test_cedar_errors_deny(monkeypatch):
    """Cedar 返回 allow 但带求值错误 → deny（policy_error）。"""
    client = _client(monkeypatch, POLICY_ENGINE="cedar", CEDAR_URL="http://cedar.test")
    _mock_cedar(
        monkeypatch,
        lambda _r, b: httpx.Response(
            200,
            json={"results": [{"decision": "allow", "policy_ids": ["p"], "errors": ["e"]} for _ in b["requests"]], "policy_version": "v"},
        ),
    )
    res = _check_batch(client, [ALL_ITEMS[0]])["results"][0]
    assert (res["decision"], res["reason"]) == ("deny", "policy_error")


def test_cedar_unreachable_denies(monkeypatch):
    """Cedar 不可达：批量与单条接口全部 deny，reason=policy_unavailable。"""
    client = _client(monkeypatch, POLICY_ENGINE="cedar", CEDAR_URL="http://127.0.0.1:1", CEDAR_TIMEOUT_S="0.5")
    body = _check_batch(client, ALL_ITEMS)
    assert body["engine"] == "cedar" and body["policy_version"] == "unavailable"
    assert {(r["decision"], r["reason"]) for r in body["results"]} == {("deny", "policy_unavailable")}
    r = client.post(
        "/authz/check",
        json={
            "subject": "human:1",
            "capability": "cap.crm.read",
            "principal": {"tenant": "t1"},
            "resource": {"risk_level": "read", "tenant": "t1"},
        },
    ).json()
    assert (r["decision"], r["matched"], r["engine"]) == ("deny", "policy_unavailable", "cedar")


def test_batch_validation(monkeypatch):
    """非法请求（缺租户、审批字段非布尔、超过条数上限）返回 422，调用方按 deny 处理；空批返回空结果。"""
    client = _client(monkeypatch, POLICY_ENGINE="baseline")
    no_tenant = {**ALL_ITEMS[0], "resource": {k: v for k, v in ALL_ITEMS[0]["resource"].items() if k != "tenant"}}
    stringly = {**ALL_ITEMS[2], "context": {"approved": "true", "taint": []}}
    assert client.post("/authz/check-batch", json={"requests": [no_tenant]}).status_code == 422
    assert client.post("/authz/check-batch", json={"requests": [stringly]}).status_code == 422
    too_many = [ALL_ITEMS[0]] * (main.MAX_BATCH + 1)
    assert client.post("/authz/check-batch", json={"requests": too_many}).status_code == 422
    assert client.post("/authz/check-batch", json={"requests": [ALL_ITEMS[0]] * main.MAX_BATCH}).status_code == 200
    assert _check_batch(client, [])["results"] == []


# ---- 引擎选择与生产约束 ----
def test_engine_settings():
    assert main.load_settings({}).engine == "cedar", "cedar is the default; baseline only when set explicitly"
    assert main.load_settings({"POLICY_ENGINE": "baseline"}).engine == "baseline"
    assert main.load_settings({"POLICY_ENGINE": "cedar", "POLICY_ENV": "prod", "POLICY_SERVICE_TOKEN": "t"}).engine == "cedar"
    assert main.load_settings({"CEDAR_URL": "http://x:1/"}).cedar_url == "http://x:1"
    with pytest.raises(RuntimeError, match="prod"):
        main.load_settings({"POLICY_ENGINE": "baseline", "POLICY_ENV": "prod"})
    with pytest.raises(RuntimeError):
        main.load_settings({"POLICY_ENGINE": "opa"})
    # 统一环境变量：PLATFORM_ENV=prod 同样按生产处理（baseline 禁止、服务令牌必配）
    with pytest.raises(RuntimeError, match="prod"):
        main.load_settings({"POLICY_ENGINE": "baseline", "PLATFORM_ENV": "prod", "POLICY_SERVICE_TOKEN": "t"})
    with pytest.raises(RuntimeError, match="POLICY_SERVICE_TOKEN"):
        main.load_settings({"PLATFORM_ENV": "prod"})
    assert main.load_settings({"PLATFORM_ENV": "prod", "POLICY_ENV": "dev", "POLICY_SERVICE_TOKEN": "t"}).policy_env == "prod"
    assert main.load_settings({"PLATFORM_ENV": "dev"}).policy_env == "dev"


def test_service_token_required_when_configured(monkeypatch):
    """评审 #7：配置 POLICY_SERVICE_TOKEN 后 /authz/check-batch、/authz/check、/authz/decisions 须带正确的服务令牌。"""
    monkeypatch.setattr(main, "settings", main.load_settings({"POLICY_ENGINE": "baseline", "POLICY_SERVICE_TOKEN": "svc-secret"}))
    client = TestClient(main.app)
    legacy = {"subject": "u", "capability": "cap.x", "principal": {"tenant": "t1"}}
    legacy["resource"] = {"risk_level": "read", "tenant": "t1"}
    batch = {"requests": [ALL_ITEMS[0]]}
    calls = (("post", "/authz/check-batch", batch), ("post", "/authz/check", legacy), ("get", "/authz/decisions", None))
    for method, path, body in calls:
        kw = {"json": body} if body is not None else {}
        assert getattr(client, method)(path, **kw).status_code == 401
        assert getattr(client, method)(path, headers={"X-Policy-Service-Token": "wrong"}, **kw).status_code == 401
        assert getattr(client, method)(path, headers={"X-Policy-Service-Token": "svc-secret"}, **kw).status_code == 200
    assert client.get("/healthz").status_code == 200


def test_prod_refuses_to_start_with_baseline():
    """POLICY_ENV=prod + POLICY_ENGINE=baseline：导入应用即失败（uvicorn 无法启动）。"""
    env = {**os.environ, "POLICY_ENGINE": "baseline", "POLICY_ENV": "prod"}
    r = subprocess.run([sys.executable, "-c", "import policy_service.main"], env=env, capture_output=True, text=True, timeout=60)  # noqa: S603
    assert r.returncode != 0 and "POLICY_ENGINE=baseline is not allowed" in r.stderr
    env["POLICY_ENGINE"] = "cedar"
    env["POLICY_SERVICE_TOKEN"] = "svc-secret"  # noqa: S105 —— 测试值
    ok = subprocess.run([sys.executable, "-c", "import policy_service.main"], env=env, capture_output=True, text=True, timeout=60)  # noqa: S603
    assert ok.returncode == 0, ok.stderr
