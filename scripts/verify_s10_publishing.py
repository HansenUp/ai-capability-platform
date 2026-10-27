"""真实临时 Console→Runtime 发布链，Langfuse 只读；模型清单是显式本机桩。"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx
from devkit import LANGFUSE, LF_AUTH, LF_PK, LF_SK
from verify_s10_node_control import ModelInfo, port, wait_until

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true")
    args = parser.parse_args()
    assert urlsplit(LANGFUSE).hostname in {"localhost", "127.0.0.1", "::1"}
    reference = re.findall(r"langfuse:[A-Za-z0-9_./@-]+", (ROOT / "docs/evidence/s9/2026-10-18-integration.log").read_text())[-1]
    dataset, run_name = reference.removeprefix("langfuse:").split("/", 1)
    with httpx.Client(base_url=LANGFUSE, headers=LF_AUTH, timeout=15, trust_env=False) as client:
        response = client.get(f"/api/public/datasets/{quote(dataset, safe='')}/runs/{quote(run_name, safe='')}")
        response.raise_for_status()
        metadata = response.json()["metadata"]
    agent, version = metadata["agent_id"], metadata["agent_version"]
    temporary = Path(tempfile.mkdtemp(prefix="s10-publishing-"))
    console_port, runtime_port = port(), port()
    model = ThreadingHTTPServer(("127.0.0.1", 0), ModelInfo)
    threading.Thread(target=model.serve_forever, daemon=True).start()
    processes = []
    handles = []
    environment = os.environ | {"NO_PROXY": "localhost,127.0.0.1", "no_proxy": "localhost,127.0.0.1"}

    def launch(name, env, target, selected_port):
        handle = (temporary / f"{name}.log").open("w")
        handles.append(handle)
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", target, "--host", "127.0.0.1", "--port", str(selected_port)],
            cwd=ROOT,
            env=env,
            stdout=handle,
            stderr=subprocess.STDOUT,
        )
        processes.append(process)

    try:
        launch(
            "runtime",
            environment
            | {
                "DATABASE_URL": f"sqlite+aiosqlite:///{temporary}/runtime.db",
                "RUNTIME_ENV": "dev",
                "RUNTIME_ROLE": "all",
                "HARNESS": "mock",
                "AUTH_REQUIRED": "false",
                "RUNTIME_ADMIN_ENABLED": "true",
                "CONSOLE_URL": "",
                "REDIS_URL": "",
                "TOOL_DISPATCH": "local",
                "MODEL_GATEWAY_URL": f"http://127.0.0.1:{model.server_port}",
                "CONFIDENTIAL_MODEL_ALIASES": "model-a,model-b",
                "OTEL_SDK_DISABLED": "true",
                "WORKSPACE_DIR": str(temporary / "workspaces"),
            },
            "runtime.main:app",
            runtime_port,
        )
        launch(
            "console",
            environment
            | {
                "CONSOLE_DATABASE_URL": f"sqlite+aiosqlite:///{temporary}/console.db",
                "CONSOLE_ENV": "dev",
                "RUNTIME_ADMIN_URL": f"http://127.0.0.1:{runtime_port}",
                "CONSOLE_CLIENT_SECRET": "",
                "CONSOLE_OIDC_TOKEN_URL": "",
                "CONSOLE_EVAL_GATE": "langfuse",
                "EVAL_DATASETS": json.dumps({agent: [dataset]}),
                "EVAL_MIN_ITEMS": "12",
                "EVAL_MIN_PASS_RATE": "0.8",
                "LANGFUSE_HOST": LANGFUSE,
                "LANGFUSE_PUBLIC_KEY": LF_PK,
                "LANGFUSE_SECRET_KEY": LF_SK,
            },
            "console_api.main:app",
            console_port,
        )
        console_url = f"http://127.0.0.1:{console_port}"
        runtime_url = f"http://127.0.0.1:{runtime_port}"
        headers = {"X-Console-User": "ops", "X-Console-Role": "TENANT_ADMIN", "X-Console-Tenant": "default"}
        with (
            httpx.Client(base_url=console_url, timeout=30, headers=headers, trust_env=False) as console,
            httpx.Client(base_url=runtime_url, timeout=10, trust_env=False) as runtime,
        ):
            wait_until(lambda: runtime.get("/healthz").status_code == 200)
            wait_until(lambda: console.get("/healthz").status_code == 200)
            draft = {"agent_id": agent, "version": version, "model_alias": "model-a", "harness": "mock", "config": {"tools": []}}
            assert console.post("/agents", json=draft).status_code == 201
            assert runtime.get("/admin/agents").json() == []
            path = f"/agents/{quote(agent, safe='')}/versions/{quote(version, safe='')}"
            rejected = console.post(path + "/publish", json={"eval_run_ref": "manual", "canary_percent": 100})
            assert rejected.status_code == 409 and runtime.get("/admin/agents").json() == []
            published = console.post(
                path + "/publish", json={"eval_run_ref": reference, "canary_percent": 0, "canary_allowlist": ["human:ops"]}
            )
            assert published.status_code == 200, published.text
            promoted = console.put(path + "/canary", json={"percent": 100, "allowlist": []})
            assert promoted.status_code == 200, promoted.text
            actual = runtime.get("/admin/agents").json()
            assert any(row["agent_id"] == agent and row["status"] == "published" for row in actual)
            assert console.post("/agents", json=draft | {"system_prompt": "cannot overwrite published"}).status_code == 409
            print(
                "PASS real temporary Console->Runtime: draft does not sync; wrong reference blocks; "
                "0% pre-release then evaluated promotion",
                flush=True,
            )
            print(
                "TEST PROFILE ONLY: EVAL_MIN_ITEMS=12 for archived 12-item run; repository default remains 30; "
                "model catalogue stub, no real model call",
                flush=True,
            )
            info = {
                "console": console_url,
                "runtime": runtime_url,
                "agent_id": agent,
                "version": version,
                "reference": reference,
                "logs": str(temporary),
            }
            print(json.dumps(info), flush=True)
            if args.serve:
                threading.Event().wait(1800)
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
        for process in reversed(processes):
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        for handle in handles:
            handle.close()
        model.shutdown()
        model.server_close()


if __name__ == "__main__":
    main()
