"""临时 Runtime + 本地签名测试 IdP + Mock Harness；写工具仅使用无外部效果的替身。"""

import argparse
import base64
import json
import os
import sys
import tempfile
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from verify_s10_node_control import port, wait_until


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--browser-info", type=Path)
    parser.add_argument("--demo-no-migrations", action="store_true")
    parser.add_argument("--node-control-demo", action="store_true")
    parser.add_argument("--ticket-readonly-demo", action="store_true")
    parser.add_argument("--b3-read-demo", action="store_true")
    parser.add_argument("--analytics-readonly-demo", action="store_true")
    args = parser.parse_args()
    if args.analytics_readonly_demo and not args.ticket_readonly_demo:
        parser.error("analytics requires isolated readonly demo")
    if args.b3_read_demo and not args.ticket_readonly_demo:
        parser.error("--b3-read-demo requires --ticket-readonly-demo")
    if args.browser_info and (not args.serve or args.browser_info.exists()):
        parser.error("--browser-info requires --serve and a new output path")
    if args.ticket_readonly_demo and not args.demo_no_migrations:
        parser.error("--ticket-readonly-demo requires --demo-no-migrations")
    if args.demo_no_migrations and not (args.serve and args.browser_info):
        parser.error("--demo-no-migrations requires --serve and --browser-info")
    temporary = Path(tempfile.mkdtemp(prefix="s12-workbench-"))
    allowed_ports: set[int] = set()
    if args.demo_no_migrations:
        os.umask(0o077)

        def demo_audit(event, values):
            if event in {"subprocess.Popen", "os.system"}:
                raise RuntimeError("Demo forbids child tool processes")
            if event == "socket.connect":
                address = values[1]
                if not isinstance(address, tuple) or address[0] != "127.0.0.1" or address[1] not in allowed_ports:
                    raise RuntimeError("Demo forbids external service connections")
            if event == "socket.getaddrinfo" and values[0] not in {"127.0.0.1", "localhost", "::1"}:
                raise RuntimeError("Demo forbids external DNS resolution")

        sys.addaudithook(demo_audit)

        from alembic import command

        def forbidden_migration(*args, **kwargs):
            raise RuntimeError("Demo forbids Alembic migrations")

        command.upgrade = forbidden_migration
        command.downgrade = forbidden_migration
        command.stamp = forbidden_migration
    os.environ.update(
        DATABASE_URL=f"sqlite+aiosqlite:///{temporary}/runtime.db",
        WORKSPACE_DIR=str(temporary / "workspaces"),
        RUNTIME_ENV="dev",
        RUNTIME_ROLE="all",
        HARNESS="mock",
        CONFIDENTIAL_MODEL_ALIASES="f4-synthetic-only" if "--demo-no-migrations" in sys.argv else "",
        ISOLATED_CONVERSATIONS="1" if "--demo-no-migrations" in sys.argv else "0",
        AUTH_REQUIRED="true",
        RUNTIME_ADMIN_ENABLED="true",
        CONSOLE_URL="",
        REDIS_URL="",
        TOOL_DISPATCH="local",
        OTEL_SDK_DISABLED="false",
        OTEL_EXPORTER_OTLP_ENDPOINT="",
        OTEL_EXPORTER_OTLP_TRACES_ENDPOINT="",
        JWKS_URL="http://idp.test/realms/ai-platform/protocol/openid-connect/certs",
        JWT_ISSUER="http://idp.test/realms/ai-platform",
        JWT_AUDIENCE="ai-runtime",
        EGRESS_ALLOWLIST="http://idp.test",
    )
    if args.demo_no_migrations:
        os.environ["CONFIDENTIAL_MODEL_ALIASES"] = "synthetic-sales,synthetic-analytics,f4-synthetic-only"
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services/runtime/tests"))
    import runtime_policy_stub
    import uvicorn
    from test_auth import IdP

    from ai_contract import DoneEvent, TextEvent, ToolUseEvent, UsageEvent
    from runtime import credentials
    from runtime.api import auth
    from runtime.guard import gate
    from runtime.harness import registry
    from runtime.harness.mock import MockHarness
    from runtime.loop import toolbroker
    from runtime.main import app
    from runtime.state import db

    def fresh_schema(module, name):
        async def initialize():
            target = temporary / name
            if module.engine.url.get_backend_name() != "sqlite" or module.engine.url.database != str(target) or target.exists():
                raise RuntimeError("Demo schema initialization requires a new private temporary SQLite file")
            async with module.engine.begin() as connection:
                await connection.run_sync(module.Base.metadata.create_all)
                if module is db and args.demo_no_migrations:
                    from runtime.api import isolated_policies, isolated_views

                    await connection.run_sync(isolated_views.Base.metadata.create_all)
                    await connection.run_sync(isolated_policies.Base.metadata.create_all)
                    from runtime.api import isolated_a2a

                    await connection.run_sync(isolated_a2a.Metadata.metadata.create_all)
                    from runtime.api import isolated_observe

                    await connection.run_sync(isolated_observe.Metadata.metadata.create_all)
            if module is db and args.browser_info:
                import hashlib
                from datetime import UTC, datetime

                from runtime.api.auth import Principal
                from runtime.api.workbench import binding

                principal = Principal(subject="human:pagination", tenant_id="default", auth_method="user")
                scope = binding(principal)["scope_id"]
                async with db.SessionLocal() as session:
                    for index in range(25):
                        cid = str(uuid.UUID(int=1000 + index))
                        session.add(
                            db.Conversation(
                                conversation_id=cid,
                                scope_id=scope,
                                tenant_id="default",
                                subject=principal.subject,
                                agent_id="s12-workbench",
                            )
                        )
                        session.add(
                            db.Run(
                                run_id=f"run_pagination_{index:04d}",
                                tenant_id="default",
                                subject=principal.subject,
                                agent_id="s12-workbench",
                                agent_version="1",
                                model_alias="default-chat",
                                session_id=str(uuid.UUID(int=1000)),
                                state="completed",
                                last_seq=0,
                                created_at=datetime(2026, 1, 1, tzinfo=UTC),
                            )
                        )
                        content = json.dumps(
                            {
                                "protocol": "isolated-continuation-v1",
                                "question": "SYNTHETIC HISTORY ONLY",
                                "selected_history": [],
                                "synthetic_attachments": [],
                            },
                            sort_keys=True,
                        )
                        session.add(
                            db.ContinuationInput(
                                attempt_id=str(uuid.uuid4()),
                                conversation_id=str(uuid.UUID(int=1000)),
                                run_id=f"run_pagination_{index:04d}",
                                question="SYNTHETIC HISTORY ONLY",
                                selected_run_ids=[],
                                composed=content,
                                digest=hashlib.sha256(content.encode()).hexdigest(),
                            )
                        )
                    await session.commit()

        return initialize

    if args.demo_no_migrations:
        import runtime.main as runtime_main

        runtime_main.init_db = fresh_schema(db, "runtime.db")

    class WorkbenchScenario(MockHarness):
        def __init__(self):
            super().__init__()
            self.runs = {}

        async def open_session(self, spec):
            session = await super().open_session(spec)
            self.runs[session.session_id] = spec.run_id
            return session

        async def resume(self, spec, events):
            session = await super().resume(spec, events)
            self.runs[session.session_id] = spec.run_id
            return session

        async def run_turn(self, session, turn, tools, skills, hooks):
            user_index = max((index for index, row in enumerate(turn.messages) if row.get("role") == "user"), default=-1)
            content = turn.messages[user_index].get("content") if user_index >= 0 else ""
            if args.analytics_readonly_demo and content in {
                "SYNTHETIC_ORDER_SUMMARY",
                "SYNTHETIC_ORDER_INCOMPLETE",
                "SYNTHETIC_ORDER_EMPTY",
            }:
                results = [
                    row for row in turn.messages[user_index + 1 :] if row.get("role") == "tool" and row.get("call_id") == "orders-read"
                ]
                if not results:
                    day = {"SYNTHETIC_ORDER_SUMMARY": 1, "SYNTHETIC_ORDER_INCOMPLETE": 2, "SYNTHETIC_ORDER_EMPTY": 3}[content]
                    yield ToolUseEvent(
                        call_id="orders-read",
                        tool_name="analytics.orders",
                        input={"since": f"2026-10-{day:02}T00:00:00Z", "until": f"2026-10-{day + 1:02}T00:00:00Z"},
                    )
                else:
                    out = results[-1].get("content")
                    text = "模拟经营查询不可用，未生成成功指标。"
                    if isinstance(out, dict) and out.get("profile") == "synthetic-order-metrics-v1" and out.get("complete") is True:
                        text = (
                            f"模拟经营摘要：当前创建窗口订单数 {out['order_count']}；按读取时状态统计，金额按币种查看工具指标，不代表收入。"
                        )
                    yield TextEvent(delta=text)
                yield UsageEvent(input_tokens=0, output_tokens=0)
                yield DoneEvent(run_id=self.runs[session.session_id], stop_reason="end_turn" if results else "tool_use")
                return
            if args.demo_no_migrations and content in {"SYNTHETIC_SALES_SUMMARY", "SYNTHETIC_SALES_EMPTY", "SYNTHETIC_SALES_FAILURE"}:
                from sales_readonly_fixture import summary

                results = [
                    row for row in turn.messages[user_index + 1 :] if row.get("role") == "tool" and row.get("call_id") == "sales-read"
                ]
                if not results:
                    query = (
                        {}
                        if content == "SYNTHETIC_SALES_SUMMARY"
                        else {"q": "SYNTHETIC-FAIL" if content == "SYNTHETIC_SALES_FAILURE" else "NO-MATCH"}
                    )
                    yield ToolUseEvent(call_id="sales-read", tool_name="crm.opportunity.search", input=query)
                else:
                    yield TextEvent(delta=summary(results[-1].get("content")))
                yield UsageEvent(input_tokens=0, output_tokens=0)
                yield DoneEvent(run_id=self.runs[session.session_id], stop_reason="end_turn" if results else "tool_use")
                return
            if args.ticket_readonly_demo and content in {"SYNTHETIC_TICKET_SUMMARY", "SYNTHETIC_TICKET_FAILURE"}:
                results = [
                    row for row in turn.messages[user_index + 1 :] if row.get("role") == "tool" and row.get("call_id") == "ticket-read"
                ]
                if not results:
                    ticket_id = "SYNTHETIC-1" if content == "SYNTHETIC_TICKET_SUMMARY" else "SYNTHETIC-FAIL"
                    yield ToolUseEvent(call_id="ticket-read", tool_name="ticket.get", input={"id": ticket_id})
                else:
                    output = results[-1].get("content")
                    if (
                        isinstance(output, dict)
                        and set(output) == {"id", "title", "status"}
                        and all(isinstance(output[key], str) for key in ("id", "title", "status"))
                    ):
                        yield TextEvent(delta=f"模拟工单摘要：{output['id']} / {output['title']} / {output['status']}")
                    else:
                        yield TextEvent(delta="模拟工单查询不可用，未生成摘要。")
                yield UsageEvent(input_tokens=0, output_tokens=0)
                yield DoneEvent(run_id=self.runs[session.session_id], stop_reason="tool_use" if not results else "end_turn")
                return
            if content == "S12_E2E_CREDENTIAL_EXPIRY":
                results = [row for row in turn.messages[user_index + 1 :] if row.get("role") == "tool"]
                if not results:
                    run_id = self.runs[session.session_id]
                    async with db.SessionLocal() as stored:
                        run = await stored.get(db.Run, run_id)
                        assert run is not None
                        await credentials.store_credential(stored, run_id, run.subject, tokens["expired_alice"])
                        await stored.commit()
                    yield ToolUseEvent(call_id="expiry-answer", tool_name="request_user_input", input={"prompt": "合成过期恢复前置回答"})
                else:
                    yield TextEvent(delta="S12 同发起人凭据刷新完成：同 Run，无外部效果。")
                yield UsageEvent(input_tokens=0, output_tokens=0)
                yield DoneEvent(run_id=self.runs[session.session_id], stop_reason="tool_use" if not results else "end_turn")
                return
            if content != "S12_E2E_APPROVAL_THEN_INPUT":
                async for event in super().run_turn(session, turn, tools, skills, hooks):
                    yield event
                return
            results = [row for row in turn.messages[user_index + 1 :] if row.get("role") == "tool"]
            if not results:
                yield ToolUseEvent(call_id="synthetic-write", tool_name="ticket.close_batch", input={"ids": ["SYNTHETIC-E2E"]})
            elif len(results) == 1:
                yield ToolUseEvent(
                    call_id="synthetic-answer",
                    tool_name="request_user_input",
                    input={
                        "prompt": "补充合成工单 ID",
                        "schema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
                    },
                )
            else:
                yield TextEvent(delta="S12 自动端到端完成：仅合成工具，无外部效果。")
            yield UsageEvent(input_tokens=0, output_tokens=0)
            yield DoneEvent(run_id=session.session_id, stop_reason="tool_use" if len(results) < 2 else "end_turn")

    registry.register("mock", WorkbenchScenario())

    provider = IdP()
    auth.set_transport_for_tests(provider.transport())
    gate.set_policy_transport_for_tests(runtime_policy_stub.transport())
    writes: list[str] = []

    async def synthetic(tool, arguments, dry_run=False):
        if args.ticket_readonly_demo:
            if tool.name != "ticket.get" or dry_run or arguments not in ({"id": "SYNTHETIC-1"}, {"id": "SYNTHETIC-FAIL"}):
                raise RuntimeError("Readonly demo rejects non-allowlisted tools and arguments")
            from mcp_ticket.auth import Identity
            from mcp_ticket.synthetic_http import ReadDenied, ReadUnavailable, SyntheticCaller, SyntheticHttpTicketAdapter
            from mcp_ticket.tools import GetIn

            def ticket_service(request):
                if request.url.path.endswith("SYNTHETIC-FAIL"):
                    return httpx.Response(503)
                return httpx.Response(
                    200,
                    json={
                        "id": "SYNTHETIC-1",
                        "tenant": "demo",
                        "title": "设备无法连接",
                        "status": "open",
                        "priority": None,
                        "category": None,
                        "owner": "synthetic",
                        "requester": "synthetic",
                        "description": "不应进入模型的合成说明",
                        "records": [],
                        "records_truncated": False,
                        "customer_feedback": None,
                        "created_at": "synthetic",
                        "updated_at": "synthetic",
                        "closed_at": None,
                    },
                )

            adapter = SyntheticHttpTicketAdapter(httpx.MockTransport(ticket_service))
            caller = SyntheticCaller(Identity(sub="demo-user", tenant="demo", act_verified=True), "synthetic-demo-token")
            try:
                return adapter.get(GetIn(id=arguments["id"]), caller)
            except (ReadDenied, ReadUnavailable):
                return {"error": "synthetic ticket unavailable"}
        if dry_run:
            return {"impact_preview": {"simulation": True}}
        writes.append(tool.name)
        return {"simulation": True, "external_effect": False}

    toolbroker._dev_execute = synthetic
    if args.b3_read_demo:
        import b3_read_fixture

        b3_read_fixture.install(provider, synthetic)
    if args.demo_no_migrations and not args.ticket_readonly_demo:
        import f4_query_fixture

        f4_query_fixture.install(provider, temporary)
    if args.demo_no_migrations:
        import sales_readonly_fixture

        sales_readonly_fixture.install()
    if args.analytics_readonly_demo:
        import order_metrics_fixture

        order_metrics_fixture.install(provider, temporary)
    selected_port = port()
    allowed_ports.add(selected_port)
    if args.demo_no_migrations:
        from runtime.api import isolated_policies, isolated_views

        app.state.isolated_views = isolated_views.ENABLED
        app.include_router(isolated_views.router)
        app.state.isolated_policies = isolated_policies.ENABLED
        app.include_router(isolated_policies.router)
        from runtime.api import isolated_discovery

        async def synthetic_visibility(tenant):
            return {
                "tenant": tenant,
                "revision": 1,
                "rules": [{"agent_id": "s12-workbench", "groups": [], "roles": [], "allowlist": ["human:alice"]}]
                if tenant == "default"
                else [],
            }

        app.state.isolated_discovery = isolated_discovery.ENABLED
        app.state.isolated_visibility_reader = synthetic_visibility
        app.include_router(isolated_discovery.router)
        from runtime.api import isolated_a2a

        app.state.isolated_a2a = isolated_a2a.ENABLED
        app.include_router(isolated_a2a.router)
        from runtime.api import isolated_mcp

        app.state.isolated_mcp = isolated_mcp.ENABLED
        app.include_router(isolated_mcp.router)
        from runtime.api import isolated_observe
        from runtime.api.auth import settings as auth_settings

        app.state.isolated_observe = isolated_observe.ENABLED
        app.state.isolated_observe_bindings = {(auth_settings.jwt_issuer, "observe-test", "reporter"): ("default", "s12-workbench")}
        app.include_router(isolated_observe.router)

    tokens = {name: provider.token({"sub": name, "exp": int(time.time()) + 1800}) for name in ["alice", "bob", "pagination"]}
    for name, roles in [
        ("policy_maintainer", ["data-policy-maintainer"]),
        ("policy_reviewer_a", ["data-policy-reviewer"]),
        ("policy_reviewer_b", ["data-policy-reviewer"]),
        ("view_maintainer", ["data-view-maintainer"]),
        ("view_reviewer_a", ["data-view-reviewer"]),
        ("view_reviewer_b", ["data-view-reviewer"]),
    ]:
        tokens[name] = provider.token({"sub": name, "realm_access": {"roles": roles}, "exp": int(time.time()) + 1800})
    tokens["alice_fresh"] = provider.token({"sub": "alice", "exp": int(time.time()) + 1800, "jti": uuid.uuid4().hex})
    tokens["foreign"] = provider.token({"sub": "foreign", "tenant": "s12-foreign", "exp": int(time.time()) + 1800})
    tokens["expired_alice"] = provider.token({"sub": "alice", "exp": int(time.time()) - 120})
    tokens["feedback_admin"] = provider.token(
        {"sub": "feedback-reader", "realm_access": {"roles": ["platform-admin"]}, "exp": int(time.time()) + 1800}
    )
    tokens["observe_reporter"] = provider.token({"sub": "reporter", "client_id": "observe-test", "exp": int(time.time()) + 1800})
    tokens["observe_foreign_admin"] = provider.token(
        {"sub": "reader", "tenant": "observe-foreign", "realm_access": {"roles": ["platform-admin"]}, "exp": int(time.time()) + 1800}
    )
    if args.browser_info:
        import owner_browser_fixture

        tokens.update(owner_browser_fixture.tokens(provider))
        for name in ("alice", "bob"):
            tokens["alert_" + name] = provider.token(
                {
                    "sub": name,
                    "aud": "console-owner-test",
                    "azp": "console-test",
                    "identity_type": "user",
                    "nbf": int(time.time()) - 1,
                    "exp": int(time.time()) + 1800,
                    "realm_access": {"roles": ["alert-admin"]},
                }
            )
    token_file = temporary / "tokens.json"
    token_file.write_text(json.dumps(tokens))
    token_file.chmod(0o600)
    admin = provider.token({"realm_access": {"roles": ["platform-admin"]}})
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=selected_port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{selected_port}"
    console_server = None
    console_thread = None
    console_base = None
    feedback_server = None
    feedback_thread = None
    feedback_info = None
    feedback_methods = []
    try:
        wait_until(lambda: httpx.get(f"{base}/healthz", trust_env=False).status_code == 200)
        if args.browser_info:
            os.environ.update(
                CONSOLE_ENV="dev",
                CONSOLE_EVAL_GATE="flag",
                CONSOLE_DATABASE_URL=f"sqlite+aiosqlite:///{temporary}/console.db",
                RUNTIME_ADMIN_URL=base,
                CONSOLE_CLIENT_SECRET=uuid.uuid4().hex,
                CONSOLE_OIDC_TOKEN_URL=os.environ["JWT_ISSUER"] + "/protocol/openid-connect/token",
            )
            from console_api import runtime_client
            from console_api.main import app as console_app

            if args.demo_no_migrations:
                import console_api.main as console_main
                from console_api import db as console_db

                console_main.init_db = fresh_schema(console_db, "console.db")

            owner_browser_fixture.configure(provider)
            if args.demo_no_migrations:
                import f7_node_fixture

                from console_api.isolated_alerts import Store

                if args.node_control_demo:
                    f7_node_fixture.configure(console_app)

                console_app.state.isolated_alert_store = Store()
                console_app.state.isolated_alert_store.observe("default", "synthetic-alert", "synthetic-heartbeat-1", 1, "abnormal")

            service_token = provider.token(
                {
                    "sub": "sa-console",
                    "azp": "console-api",
                    "client_id": "console-api",
                    "preferred_username": "service-account-console-api",
                    "realm_access": {"roles": ["platform-admin"]},
                    "exp": int(time.time()) + 1800,
                }
            )
            runtime_client.RUNTIME_ADMIN_URL = base
            runtime_client.TOKEN_URL = os.environ["CONSOLE_OIDC_TOKEN_URL"]
            runtime_client._token = (service_token, time.monotonic() + 1700)
            console_port = port()
            allowed_ports.add(console_port)
            console_base = f"http://127.0.0.1:{console_port}"
            console_server = uvicorn.Server(uvicorn.Config(console_app, host="127.0.0.1", port=console_port, log_level="warning"))
            console_thread = threading.Thread(target=console_server.run, daemon=True)
            console_thread.start()
            wait_until(lambda: httpx.get(f"{console_base}/healthz", trust_env=False).status_code == 200)
            if args.node_control_demo:
                import asyncio

                async def admit_f7():
                    from console_api.db import SessionLocal

                    async with SessionLocal() as session:
                        await console_app.state.isolated_node_control.admit_fresh_nodes(session)

                asyncio.run(admit_f7())
            if not args.ticket_readonly_demo:
                owner_browser_fixture.seed(console_base)
        if args.ticket_readonly_demo:
            args.browser_info.write_text(json.dumps({"runtime": base, "console": console_base, "tokens": str(token_file)}))
            args.browser_info.chmod(0o600)
            print("PROFILE readonly-ticket mock-model mock-tool; no external effects", flush=True)
            threading.Event().wait(1800)
            return
        with httpx.Client(base_url=base, trust_env=False, timeout=10) as client:
            wait_until(lambda: client.get("/healthz").status_code == 200)
            admin_headers = {"Authorization": f"Bearer {admin}"}
            assert (
                client.post(
                    "/admin/agents",
                    headers=admin_headers,
                    json={
                        "agent_id": "s12-workbench",
                        "version": "1",
                        "harness": "mock",
                        "config": {"tools": ["echo", "ticket.close_batch"], "user_input": True},
                    },
                ).status_code
                == 201
            )
            assert (
                client.post("/admin/agents/s12-workbench/versions/1/publish", headers=admin_headers, json={"eval_passed": True}).status_code
                == 200
            )
            if args.demo_no_migrations:
                assert (
                    client.post(
                        "/admin/agents",
                        headers=admin_headers,
                        json={
                            "agent_id": "f4-data-query",
                            "version": "1",
                            "harness": "mock",
                            "model_alias": "f4-synthetic-only",
                            "config": {"tools": ["data.query"]},
                        },
                    ).status_code
                    == 201
                )
                assert (
                    client.post(
                        "/admin/agents/f4-data-query/versions/1/publish", headers=admin_headers, json={"eval_passed": True}
                    ).status_code
                    == 200
                )
            alice, bob = ({"Authorization": f"Bearer {tokens[name]}"} for name in ["alice", "bob"])

            def create(content):
                response = client.post(
                    "/v1/runs", headers=alice, json={"agent_id": "s12-workbench", "input": [{"role": "user", "content": content}]}
                )
                assert response.status_code == 202
                return response.json()["run_id"]

            def state(run_id, expected):
                return client.get(f"/v1/runs/{run_id}", headers=alice).json().get("state") == expected

            run_id = create('!tool request_user_input {"prompt":"合成回答？","schema":{"type":"string"}}')
            wait_until(lambda: state(run_id, "input-required"))
            events = client.get(f"/v1/runs/{run_id}/events", headers=alice).json()
            call = next(event["payload"]["call_id"] for event in events if event["type"] == "input.requested")
            body = {"kind": "answer", "call_id": call, "value": "synthetic answer"}
            assert client.post(f"/v1/runs/{run_id}/input", headers=bob, json=body).status_code == 403
            assert client.post(f"/v1/runs/{run_id}/input", headers=alice, json=body).status_code == 200
            wait_until(lambda: state(run_id, "completed"))
            run_id = create('!tool ticket.close_batch {"ids":["SYNTHETIC-ONLY"]}')
            wait_until(lambda: state(run_id, "auth-required"))
            events = client.get(f"/v1/runs/{run_id}/events", headers=bob).json()
            decision = next(event["payload"] for event in events if event["type"] == "tool.decision")
            body = {"call_id": decision["call_id"], "params_hash": decision["params_hash"]}
            assert client.post(f"/v1/runs/{run_id}/approve", headers=alice, json=body).status_code == 403
            assert client.post(f"/v1/runs/{run_id}/approve", headers=bob, json=body).status_code == 200
            wait_until(lambda: state(run_id, "completed"))
            assert writes == ["ticket.close_batch"]
            credential_runs = []
            for _scenario in range(2):
                credential_run = create("S12_E2E_CREDENTIAL_EXPIRY")
                wait_until(lambda run_id=credential_run: state(run_id, "input-required"))
                history = client.get(f"/v1/runs/{credential_run}/events", headers=alice).json()
                asked = next(event["payload"] for event in history if event["type"] == "input.requested")
                assert asked["kind"] == "answer"
                assert (
                    client.post(
                        f"/v1/runs/{credential_run}/input",
                        headers=alice,
                        json={"kind": "answer", "call_id": asked["call_id"], "value": "synthetic expiry preparation"},
                    ).status_code
                    == 200
                )

                def credential_pending(run_id=credential_run):
                    if not state(run_id, "input-required"):
                        return False
                    rows = client.get(f"/v1/runs/{run_id}/events", headers=alice).json()
                    requests = [row["payload"] for row in rows if row["type"] == "input.requested"]
                    return requests[-1].get("kind") == "credential"

                wait_until(credential_pending)
                credential_runs.append(credential_run)
            print(
                "PASS actual isolated Runtime: answer by initiator -> same Run completed; self approval 403; "
                "other signed test user approves -> one synthetic write; no external effects",
                flush=True,
            )
            print(
                f"PROFILE Runtime={base}; Agent=s12-workbench; test_token_file={token_file}; "
                "Mock Harness/policy/IdP, no production identity claim",
                flush=True,
            )
            if args.browser_info:
                from fastapi import FastAPI, Request
                from fastapi.responses import JSONResponse

                feedback_agent = "s12-feedback-audit"
                with httpx.Client(base_url=console_base, trust_env=False, timeout=10) as control:
                    assert (
                        control.post(
                            "/agents", json={"agent_id": feedback_agent, "version": "1", "harness": "mock", "config": {"tools": []}}
                        ).status_code
                        == 201
                    )
                    assert control.post(f"/agents/{feedback_agent}/versions/1/publish", json={"eval_passed": True}).status_code == 200
                created = client.post(
                    "/v1/runs",
                    headers=alice,
                    json={"agent_id": feedback_agent, "input": [{"role": "user", "content": "S12 feedback audit synthetic setup"}]},
                )
                assert created.status_code == 202
                feedback_run = created.json()["run_id"]
                wait_until(lambda: state(feedback_run, "completed"))
                day = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
                feedback_info = {
                    "agent": feedback_agent,
                    "run_id": feedback_run,
                    "since": day.isoformat(),
                    "until": (day + timedelta(days=1)).isoformat(),
                }
                v2_agent = feedback_agent + "-v2"
                with httpx.Client(base_url=console_base, trust_env=False, timeout=10) as control:
                    assert (
                        control.post(
                            "/agents", json={"agent_id": v2_agent, "version": "1", "harness": "mock", "config": {"tools": []}}
                        ).status_code
                        == 201
                    )
                    assert control.post(f"/agents/{v2_agent}/versions/1/publish", json={"eval_passed": True}).status_code == 200
                created_v2 = client.post(
                    "/v1/runs",
                    headers=alice,
                    json={"agent_id": v2_agent, "input": [{"role": "user", "content": "isolated aggregation v2 fixture"}]},
                )
                assert created_v2.status_code == 202
                v2_run = created_v2.json()["run_id"]
                wait_until(lambda: state(v2_run, "completed"))
                feedback_info["v2"] = {"agent": v2_agent, "run_id": v2_run}
                fake_langfuse = FastAPI()
                trusted_scores = {}
                feedback_secret = uuid.uuid4().hex
                feedback_authorization = "Basic " + base64.b64encode(f"feedback-public:{feedback_secret}".encode()).decode()
                score = {
                    "id": "synthetic-feedback-score",
                    "name": "user_feedback",
                    "traceId": "synthetic-feedback-trace",
                    "value": 1,
                    "dataType": "NUMERIC",
                    "updatedAt": datetime.now(UTC).isoformat(),
                    "comment": "PRIVATE-FEEDBACK-COMMENT",
                    "metadata": {"run_id": feedback_run, "subject": "human:alice", "verified": True, "correction": "PRIVATE-CORRECTION"},
                }

                @fake_langfuse.middleware("http")
                async def feedback_read_only(request: Request, call_next):
                    feedback_methods.append(request.method)
                    if request.method != "GET":
                        return JSONResponse({"detail": "read-only fixture"}, status_code=405)
                    if request.url.path != "/healthz" and request.headers.get("authorization") != feedback_authorization:
                        return JSONResponse({"detail": "isolated source auth failed"}, status_code=401)
                    return await call_next(request)

                @fake_langfuse.get("/healthz")
                async def source_health():
                    return {"status": "ok"}

                @fake_langfuse.get("/api/public/v2/scores")
                async def source_scores(page: int = 1, limit: int = 100):
                    rows = [score, *list(trusted_scores.values())]
                    return {
                        "data": rows[(page - 1) * limit : page * limit],
                        "meta": {"page": page, "limit": limit, "totalItems": len(rows), "totalPages": (len(rows) + limit - 1) // limit},
                    }

                @fake_langfuse.get("/api/public/traces/synthetic-feedback-trace")
                async def source_trace():
                    return {
                        "id": "synthetic-feedback-trace",
                        "sessionId": feedback_run,
                        "metadata": {
                            "attributes": {
                                "ai.tenant_id": "default",
                                "ai.agent_id": feedback_agent,
                                "ai.run_id": feedback_run,
                                "ai.subject": "human:alice",
                            }
                        },
                    }

                @fake_langfuse.get("/api/public/traces/{trace_id}")
                async def trusted_read_trace(trace_id: str):
                    payload = next((row for row in list(trusted_scores.values()) if row["traceId"] == trace_id), None)
                    if payload is None:
                        return JSONResponse({"detail": "not found"}, status_code=404)
                    meta = payload["metadata"]
                    return {
                        "id": trace_id,
                        "sessionId": meta["run_id"],
                        "metadata": {"attributes": {"ai." + key: meta[key] for key in ("tenant_id", "agent_id", "run_id", "subject")}},
                    }

                feedback_port = port()
                allowed_ports.add(feedback_port)
                feedback_base = f"http://127.0.0.1:{feedback_port}"
                os.environ.update(LANGFUSE_HOST=feedback_base, LANGFUSE_PUBLIC_KEY="feedback-public", LANGFUSE_SECRET_KEY=feedback_secret)
                feedback_server = uvicorn.Server(uvicorn.Config(fake_langfuse, host="127.0.0.1", port=feedback_port, log_level="warning"))
                feedback_thread = threading.Thread(target=feedback_server.run, daemon=True)
                feedback_thread.start()
                wait_until(lambda: httpx.get(feedback_base + "/healthz", trust_env=False).status_code == 200)
                from observe_adapter import feedback_protocol
                from observe_adapter.main import app as observe_app
                from runtime.api import feedback_writes

                trusted_posts = []

                def trusted_source(request):
                    assert request.headers.get("authorization") == feedback_authorization
                    if request.method == "POST":
                        assert request.url.path == "/api/public/scores"
                        payload = json.loads(request.content)
                        assert payload["traceId"] != "0" * 32
                        assert all(
                            row["metadata"]["run_id"] == payload["metadata"]["run_id"]
                            for row in trusted_scores.values()
                            if row["traceId"] == payload["traceId"]
                        )
                        trusted_posts.append(payload["id"])
                        assert trusted_posts.count(payload["id"]) == 1
                        trusted_scores[payload["id"]] = payload
                        return httpx.Response(200, json={"id": payload["id"]})
                    assert request.method == "GET"
                    if request.url.path.startswith("/api/public/v2/scores/"):
                        payload = trusted_scores.get(request.url.path.rsplit("/", 1)[-1])
                        return httpx.Response(200, json=payload) if payload else httpx.Response(404)
                    if request.url.path.startswith("/api/public/traces/"):
                        trace_id = request.url.path.rsplit("/", 1)[-1]
                        payload = next((row for row in trusted_scores.values() if row["traceId"] == trace_id), None)
                        if payload:
                            meta = payload["metadata"]
                            return httpx.Response(
                                200,
                                json={
                                    "id": trace_id,
                                    "sessionId": meta["run_id"],
                                    "metadata": {
                                        "attributes": {"ai." + key: meta[key] for key in ("tenant_id", "subject", "run_id", "agent_id")}
                                    },
                                },
                            )
                    return httpx.Response(404)

                os.environ.update(
                    FEEDBACK_LOCAL_PROTOCOL="1",
                    FEEDBACK_LOCAL_EVIDENCE_READ="1",
                    OBSERVE_ENV="dev",
                    FEEDBACK_LOCAL_SERVICE_TOKEN=uuid.uuid4().hex + uuid.uuid4().hex,
                    FEEDBACK_LOCAL_OBSERVE_URL="http://127.0.0.1:1",
                )
                os.environ["EGRESS_ALLOWLIST"] = os.environ.get("EGRESS_ALLOWLIST", "") + ",http://127.0.0.1:1"
                feedback_protocol._transport = httpx.MockTransport(trusted_source)
                feedback_writes._transport = httpx.ASGITransport(app=observe_app)
                args.browser_info.write_text(
                    json.dumps(
                        {
                            "runtime": base,
                            "console": console_base,
                            "tokens": str(token_file),
                            "credential_runs": credential_runs,
                            "feedback": feedback_info,
                        }
                    )
                )
                args.browser_info.chmod(0o600)
            if args.serve:
                threading.Event().wait(1800)
    except KeyboardInterrupt:
        print("STOP isolated workbench fixture requested", flush=True)
    finally:
        # A terminal interrupt can reach both parent and child; do not interrupt cleanup twice.
        import signal

        signal.signal(signal.SIGINT, signal.SIG_IGN)
        if console_server is not None:
            console_server.should_exit = True
        if console_thread is not None:
            console_thread.join(timeout=10)
        if feedback_server is not None:
            feedback_server.should_exit = True
        if feedback_thread is not None:
            feedback_thread.join(timeout=10)
        server.should_exit = True
        thread.join(timeout=10)
        if console_server is not None:
            close_f7 = getattr(console_app.state, "close_f7_fixture", None)
            if close_f7 is not None:
                close_f7()
            alert_store = getattr(console_app.state, "isolated_alert_store", None)
            if alert_store is not None:
                alert_store.close()
                print("CLEANUP isolated alert SQLite removed", flush=True)
        (temporary / "f4-synthetic.sqlite").unlink(missing_ok=True)
        token_file.unlink(missing_ok=True)
        if args.browser_info:
            args.browser_info.unlink(missing_ok=True)
        if (
            thread.is_alive()
            or (console_thread is not None and console_thread.is_alive())
            or (feedback_thread is not None and feedback_thread.is_alive())
            or any(method != "GET" for method in feedback_methods)
        ):
            raise RuntimeError("temporary Runtime or Console did not stop")
        if args.ticket_readonly_demo:
            import shutil

            shutil.rmtree(temporary)
            print("CLEANUP readonly ticket temporary fixture removed", flush=True)


if __name__ == "__main__":
    main()
