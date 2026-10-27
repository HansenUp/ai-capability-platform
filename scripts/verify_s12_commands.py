"""隔离 Console HTTP 指令闭环；模拟节点仅生成回执，绝不调用 Runtime 执行器。"""

import argparse
import os
import secrets
import tempfile
import threading
from pathlib import Path

import httpx
from verify_s10_node_control import port, wait_until


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--lose-browser-response", action="store_true")
    args = parser.parse_args()
    temporary = Path(tempfile.mkdtemp(prefix="s12-command-fixture-"))
    selected_port = port()
    token = secrets.token_urlsafe()
    os.environ.update(
        CONSOLE_ENV="dev",
        CONSOLE_DATABASE_URL=f"sqlite+aiosqlite:///{temporary}/console.db",
        CONSOLE_CLIENT_SECRET="",
        CONSOLE_NODE_TOKEN=token,
        CONSOLE_NODE_JWKS_URL="",
        RUNTIME_ADMIN_URL="http://127.0.0.1:1",
    )
    import uvicorn

    from console_api.main import app

    if args.lose_browser_response:
        from fastapi.responses import JSONResponse

        lost = False

        @app.middleware("http")
        async def simulate_lost_id(request, call_next):
            nonlocal lost
            response = await call_next(request)
            if (
                not lost
                and request.headers.get("X-S12-Response-Profile") == "lose-id"
                and request.method == "POST"
                and request.url.path.endswith("/commands")
                and response.status_code == 202
            ):
                lost = True
                print("SIMULATION committed one command but dropped its response ID; never retry", flush=True)
                return JSONResponse({}, status_code=202)
            return response

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=selected_port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{selected_port}"
    node_id = "s12-isolated-simulated"
    headers = {"X-Console-User": "local-tester", "X-Console-Tenant": "default", "X-Console-Role": "PLATFORM_ADMIN"}
    try:
        with (
            httpx.Client(base_url=base, headers=headers, trust_env=False, timeout=5) as console,
            httpx.Client(base_url=base, headers={"Authorization": f"Bearer {token}"}, trust_env=False, timeout=5) as node,
        ):
            wait_until(lambda: console.get("/healthz").status_code == 200)
            assert (
                node.post("/nodes/register", json={"node_id": node_id, "labels": ["general"], "version": "simulation-only"}).status_code
                == 200
            )

            def simulate() -> list[str]:
                response = node.get(f"/nodes/{node_id}/commands")
                response.raise_for_status()
                handled = []
                for command in response.json():
                    status = {"reload_config": "ok", "reload_skills": "ok", "switch_model": "error", "graceful_restart": "unsupported"}[
                        command["type"]
                    ]
                    path = f"/nodes/{node_id}/commands/{command['id']}"
                    ack = {"status": status, "detail": {"simulation": True, "executed": False}}
                    result = node.post(path + "/ack", json=ack)
                    assert result.status_code == 200 and result.json()["status"] == status
                    repeat = node.post(path + "/ack", json={"status": "error", "detail": {"ignored_duplicate": True}})
                    assert repeat.json() == result.json()
                    assert console.get(path).json() == result.json()
                    assert console.get(path, headers={"X-Console-Tenant": "foreign"}).status_code == 404
                    handled.append(command["id"])
                return handled

            cases = {
                "reload_config": {"poll_interval_s": 5.0},
                "reload_skills": {"skills": [{"name": "synthetic", "version": "1", "manifest_sha256": "a" * 64}]},
                "switch_model": {
                    "agent_id": "synthetic",
                    "base_version": "1",
                    "expected_version": "1",
                    "target_version": "2",
                    "model_alias": "mock",
                },
                "graceful_restart": {"timeout_s": 30.0},
            }
            for kind, arguments in cases.items():
                response = console.post(f"/nodes/{node_id}/commands", json={"type": kind, "args": arguments})
                assert response.status_code == 202
                command_id = response.json()["id"]
                assert console.get(f"/nodes/{node_id}/commands/{command_id}").json()["status"] == "pending"
                assert simulate() == [command_id]
                assert simulate() == []
            denied = console.post(
                f"/nodes/{node_id}/commands",
                headers={"X-Console-Role": "TENANT_ADMIN"},
                json={"type": "graceful_restart", "args": {"timeout_s": 30}},
            )
            assert denied.status_code == 403
            print(
                "PASS isolated HTTP: four command types 202 -> pending -> simulated ok/error/unsupported; "
                "duplicate ACK preserves first result; tenant 404; restart role 403",
                flush=True,
            )
            print(
                "PROFILE simulation only; no Runtime executor, model switch, Skill materialization, drain or restart. "
                f"Console={base}; node={node_id}",
                flush=True,
            )
            if args.serve:
                while True:
                    node.post(f"/nodes/{node_id}/heartbeat", json={"version": "simulation-only"}).raise_for_status()
                    simulate()
                    threading.Event().wait(0.2)
    except KeyboardInterrupt:
        print("STOP isolated fixture requested; closing temporary Console only", flush=True)
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        if thread.is_alive():
            raise RuntimeError("temporary Console did not shut down")


if __name__ == "__main__":
    main()
