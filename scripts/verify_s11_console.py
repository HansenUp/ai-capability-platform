"""S11 独立本机 Console / Runtime 验证夹具，不访问模型或生产环境。"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx
from verify_s10_node_control import port, wait_until

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--langfuse", action="store_true")
    args = parser.parse_args()
    temporary = Path(tempfile.mkdtemp(prefix="s11-console-"))
    runtime_port, console_port = port(), port()
    processes = []
    handles = []
    environment = os.environ | {
        "NO_PROXY": "localhost,127.0.0.1",
        "OTEL_SDK_DISABLED": "false",
        "OTEL_EXPORTER_OTLP_ENDPOINT": "",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "",
        "RUNTIME_ENV": "dev",
        "RUNTIME_ROLE": "all",
        "HARNESS": "mock",
        "AUTH_REQUIRED": "false",
        "RUNTIME_ADMIN_ENABLED": "true",
        "CONSOLE_URL": "",
        "REDIS_URL": "",
        "TOOL_DISPATCH": "local",
        "MODEL_GATEWAY_URL": "http://127.0.0.1:4000",
        "CAPABILITY_GATEWAY_URL": "http://127.0.0.1:8082",
        "POLICY_URL": "http://127.0.0.1:8083",
        "OBSERVE_ADAPTER_URL": "http://127.0.0.1:8084",
        "CONSOLE_ENV": "dev",
        "CONSOLE_EVAL_GATE": "flag",
        "CONSOLE_CLIENT_SECRET": "",
        "CONSOLE_OIDC_TOKEN_URL": "",
        "DATABASE_URL": f"sqlite+aiosqlite:///{temporary}/runtime.db",
        "CONSOLE_DATABASE_URL": f"sqlite+aiosqlite:///{temporary}/console.db",
        "RUNTIME_ADMIN_URL": f"http://127.0.0.1:{runtime_port}",
        "WORKSPACE_DIR": str(temporary / "workspaces"),
    }
    lf_headers = {}
    lf_host = ""
    evaluation_reference = ""
    evaluation_metadata = {}
    if args.langfuse:
        import re

        from devkit import LANGFUSE, LF_AUTH, LF_PK, LF_SK

        assert urlsplit(LANGFUSE).hostname in {"localhost", "127.0.0.1", "::1"}
        lf_host, lf_headers = LANGFUSE, LF_AUTH
        evidence = (ROOT / "docs/evidence/s9/2026-10-18-integration.log").read_text()
        evaluation_reference = re.findall(r"langfuse:[A-Za-z0-9_./@-]+", evidence)[-1]
        dataset, run_name = evaluation_reference.removeprefix("langfuse:").split("/", 1)
        with httpx.Client(base_url=lf_host, headers=lf_headers, timeout=15, trust_env=False) as client:
            response = client.get(f"/api/public/datasets/{quote(dataset, safe='')}/runs/{quote(run_name, safe='')}")
            response.raise_for_status()
            evaluation_metadata = response.json()["metadata"]
        environment |= {
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": f"{lf_host.rstrip('/')}/api/public/otel/v1/traces",
            "OTEL_EXPORTER_OTLP_TRACES_HEADERS": "Authorization=" + quote(lf_headers["Authorization"], safe=""),
            "CONSOLE_EVAL_GATE": "langfuse",
            "EVAL_DATASETS": json.dumps({evaluation_metadata["agent_id"]: [dataset]}),
            "EVAL_MIN_ITEMS": "30",
            "EVAL_MIN_PASS_RATE": "0.8",
            "LANGFUSE_HOST": lf_host,
            "LANGFUSE_PUBLIC_KEY": LF_PK,
            "LANGFUSE_SECRET_KEY": LF_SK,
        }
    try:
        services = [("runtime", "runtime.main:app", runtime_port), ("console", "console_api.main:app", console_port)]
        for name, target, selected_port in services:
            handle = (temporary / f"{name}.log").open("w")
            handles.append(handle)
            processes.append(
                subprocess.Popen(
                    [sys.executable, "-m", "uvicorn", target, "--host", "127.0.0.1", "--port", str(selected_port)],
                    cwd=ROOT,
                    env=environment,
                    stdout=handle,
                    stderr=subprocess.STDOUT,
                )
            )
        runtime_url, console_url = f"http://127.0.0.1:{runtime_port}", f"http://127.0.0.1:{console_port}"
        headers = {"X-Console-User": "ops", "X-Console-Role": "TENANT_ADMIN", "X-Console-Tenant": "default"}
        with (
            httpx.Client(base_url=console_url, headers=headers, timeout=15, trust_env=False) as console,
            httpx.Client(base_url=runtime_url, headers={"X-AI-Subject": "human:ops"}, timeout=15, trust_env=False) as runtime,
        ):
            wait_until(lambda: runtime.get("/healthz").status_code == 200)
            wait_until(lambda: console.get("/healthz").status_code == 200)
            config = {"tools": ["echo"], "skills": [], "redlines": {"deny_tools": ["ticket.update"]}, "extension": {"keep": True}}
            for version in ["1", "2"]:
                draft = {
                    "agent_id": "s11-local",
                    "version": version,
                    "system_prompt": f"local persona {version}",
                    "harness": "mock",
                    "config": config,
                }
                assert console.post("/agents", json=draft).status_code == 201
            path = "/agents/s11-local/versions/1"
            release = {"canary_percent": 0, "canary_allowlist": ["human:ops"]} if args.langfuse else {"eval_passed": True}
            assert console.post(path + "/publish", json=release).status_code == 200
            assert console.post("/agents", json={"agent_id": "s11-local", "version": "1"}).status_code == 409
            response = runtime.post(
                "/v1/messages",
                json={"agent_id": "s11-local", "messages": [{"role": "user", "content": '!tool echo {"text":"local debug fixture"}'}]},
            )
            assert response.status_code == 200, response.text
            runs = runtime.get("/v1/runs", params={"agent_id": "s11-local"}).json()["items"]
            assert runs
            run = runs[0]
            events = runtime.get(f"/v1/runs/{run['run_id']}/events").json()
            assert events and any(event["type"] == "tool.decision" for event in events)
            denied = runtime.get(f"/v1/runs/{run['run_id']}/events", headers={"X-AI-Tenant": "foreign"})
            assert denied.status_code == 404
            tail = runtime.get(f"/v1/runs/{run['run_id']}/events", params={"since_seq": events[-1]["seq"]})
            assert tail.json() == []
            stream = runtime.get(f"/v1/runs/{run['run_id']}/stream", params={"since_seq": run["last_seq"]})
            frames = [json.loads(line[5:]) for line in stream.text.splitlines() if line.startswith("data:")]
            trace_id = next(frame["trace_id"] for frame in frames if frame.get("type") == "done")
            assert isinstance(trace_id, str) and len(trace_id) == 32 and int(trace_id, 16) > 0
            if args.langfuse:
                with httpx.Client(base_url=lf_host, headers=lf_headers, timeout=15, trust_env=False) as client:
                    wait_until(lambda: client.get(f"/api/public/traces/{trace_id}").status_code == 200)
                    trace = client.get(f"/api/public/traces/{trace_id}").json()
                    assert trace["id"] == trace_id and trace["sessionId"] == run["run_id"] and trace["name"] == "run s11-local"
                evaluation_agent, evaluation_version = evaluation_metadata["agent_id"], evaluation_metadata["agent_version"]
                assert console.post("/agents", json={"agent_id": evaluation_agent, "version": evaluation_version}).status_code == 201
                result = console.get(
                    f"/agents/{evaluation_agent}/versions/{evaluation_version}/evaluation", params={"eval_run_ref": evaluation_reference}
                ).json()
                assert result["total_items"] == 12 and result["passed_items"] == 12 and result["status"] == "failed"
                assert result["policy"]["min_items"] == 30
                print(
                    "PASS real local Langfuse ingestion: Trace ID / Run session / root name match; evaluation 12/12 below default 30",
                    flush=True,
                )
            print(
                "PASS real local Console->Runtime draft/publish; immutable version; "
                "mock Run tool decision/events; foreign tenant 404; since_seq; nonzero OTel Trace in SSE done",
                flush=True,
            )
            profile = "langfuse / 0% evaluator prerelease" if args.langfuse else "development flag / no export"
            print(f"PROFILE: {profile}; mock Harness; no real model or production authentication claim", flush=True)
            print(
                json.dumps(
                    {
                        "runtime": runtime_url,
                        "console": console_url,
                        "run_id": run["run_id"],
                        "trace_id": trace_id,
                        "evaluation_reference": evaluation_reference,
                        "logs": str(temporary),
                    }
                ),
                flush=True,
            )
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


if __name__ == "__main__":
    main()
