"""S10.3 本机安全契约验收：固定版本模型切换与受管进程排空重启。"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import redis

ROOT = Path(__file__).resolve().parent.parent


def port() -> int:
    with socket.socket() as connection:
        connection.bind(("127.0.0.1", 0))
        return int(connection.getsockname()[1])


def wait_until(predicate, timeout: float = 40) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except (httpx.HTTPError, ValueError, KeyError):
            pass
        time.sleep(0.05)
    raise AssertionError("condition did not become true")


class ModelInfo(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        data = {"data": [{"model_name": alias, "litellm_params": {"model": "test/" + alias}} for alias in ("model-a", "model-b")]}
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


def main() -> None:
    temporary = Path(tempfile.mkdtemp(prefix="s10-node-control-"))
    print(f"evidence directory: {temporary}", flush=True)
    docker = shutil.which("docker")
    assert docker is not None, "Docker is required for the isolated Redis test instance"
    redis_port, console_port, api_port = port(), port(), port()
    redis_name = "s10-control-" + uuid.uuid4().hex[:8]
    processes: list[subprocess.Popen] = []
    handles = []

    def launch(name: str, command: list[str], environment: dict[str, str]):
        handle = (temporary / f"{name}.log").open("w")
        handles.append(handle)
        process = subprocess.Popen(command, cwd=ROOT, env=environment, stdout=handle, stderr=subprocess.STDOUT)
        processes.append(process)
        return process

    def pids(name: str):
        return re.findall(r"managed node child pid=(\d+)", (temporary / f"{name}.log").read_text())

    model = ThreadingHTTPServer(("127.0.0.1", 0), ModelInfo)
    threading.Thread(target=model.serve_forever, daemon=True).start()
    environment = os.environ | {"NO_PROXY": "localhost,127.0.0.1", "no_proxy": "localhost,127.0.0.1"}
    console_url = f"http://127.0.0.1:{console_port}"
    api_url = f"http://127.0.0.1:{api_port}"
    try:
        redis_process = launch(
            "redis",
            [docker, "run", "--rm", "--pull", "never", "--name", redis_name, "-p", f"127.0.0.1:{redis_port}:6379", "redis:7-alpine"],
            environment,
        )

        def redis_ready():
            if redis_process.poll() is not None:
                raise RuntimeError(f"isolated Redis exited; inspect {temporary / 'redis.log'}")
            try:
                with redis.Redis(host="127.0.0.1", port=redis_port, socket_connect_timeout=1, socket_timeout=1) as client:
                    return client.ping()
            except redis.RedisError:
                return False

        wait_until(redis_ready)
        launch(
            "console",
            [sys.executable, "-m", "uvicorn", "console_api.main:app", "--host", "127.0.0.1", "--port", str(console_port)],
            environment
            | {
                "CONSOLE_DATABASE_URL": f"sqlite+aiosqlite:///{temporary}/console.db",
                "CONSOLE_ENV": "dev",
                "CONSOLE_NODE_TOKEN": "local-s10-node",
            },
        )
        with (
            httpx.Client(base_url=console_url, timeout=5, trust_env=False) as console,
            httpx.Client(base_url=api_url, timeout=20, trust_env=False) as api,
        ):
            wait_until(lambda: console.get("/healthz").status_code == 200)
            node_environment = environment | {
                "DATABASE_URL": f"sqlite+aiosqlite:///{temporary}/runtime.db",
                "RUNTIME_ENV": "dev",
                "HARNESS": "mock",
                "AUTH_REQUIRED": "false",
                "NODE_ID": "s10-managed-api",
                "CONSOLE_URL": console_url,
                "NODE_CONSOLE_TOKEN": "local-s10-node",
                "NODE_POLL_INTERVAL_S": "1",
                "MOCK_TURN_DELAY_S": "3",
                "MODEL_GATEWAY_URL": f"http://127.0.0.1:{model.server_port}",
                "CONFIDENTIAL_MODEL_ALIASES": "model-a,model-b",
                "REDIS_URL": f"redis://127.0.0.1:{redis_port}/0",
                "WORKSPACE_DIR": str(temporary / "workspaces"),
                "OTEL_SDK_DISABLED": "true",
                "REAPER_S": "3600",
            }
            launch("api", [sys.executable, "-m", "runtime.node", "--role", "all", "--port", str(api_port)], node_environment)
            wait_until(lambda: api.get("/healthz").status_code == 200)
            admin = {"X-Console-Role": "PLATFORM_ADMIN"}

            def command(kind: str, args: dict[str, Any], node: str = "s10-managed-api"):
                response = console.post(f"/nodes/{node}/commands", json={"type": kind, "args": args}, headers=admin)
                response.raise_for_status()
                return response.json()["id"]

            def receipt(command_id: str, node: str = "s10-managed-api"):
                response = console.get(f"/nodes/{node}/commands/{command_id}", headers=admin)
                response.raise_for_status()
                return response.json()

            def chat(version=None):
                body = {"agent_id": "switch-test", "messages": [{"role": "user", "content": "local test"}], "stream": False}
                if version:
                    body["agent_version"] = version
                response = api.post("/v1/messages", json=body)
                response.raise_for_status()
                run_id = response.json()["run_id"]
                available = []

                def read_result():
                    result = api.get(f"/v1/runs/{run_id}")
                    if result.status_code == 200:
                        available.append(result)
                        return True
                    return False

                wait_until(read_result)
                result = available[-1]
                result.raise_for_status()
                assert result.json()["state"] == "completed", result.text
                return result.json()

            for version, alias in (("1", "model-a"), ("2", "model-b")):
                response = api.post(
                    "/admin/agents",
                    json={
                        "agent_id": "switch-test",
                        "version": version,
                        "model_alias": alias,
                        "harness": "mock",
                        "system_prompt": "same",
                        "config": {},
                    },
                )
                assert response.status_code == 201, response.text
                response = api.post(f"/admin/agents/switch-test/versions/{version}/publish", json={"eval_passed": True})
                assert response.status_code == 200, response.text
            with ThreadPoolExecutor() as pool:
                old_future = pool.submit(chat)
                wait_until(lambda: bool(api.get("/v1/runs", params={"agent_id": "switch-test", "state": "working"}).json()["items"]))
                switch_id = command(
                    "switch_model",
                    {
                        "agent_id": "switch-test",
                        "base_version": "2",
                        "expected_version": "2",
                        "target_version": "1",
                        "model_alias": "model-a",
                    },
                )
                wait_until(lambda: receipt(switch_id)["status"] != "pending")
                assert receipt(switch_id)["status"] == "ok", receipt(switch_id)
                old = old_future.result()
                assert old["model_alias"] == "model-b"
            assert chat()["model_alias"] == "model-a" and chat("2")["model_alias"] == "model-b"
            assert api.get(f"/v1/runs/{old['run_id']}").json()["model_alias"] == "model-b"
            print("PASS model switch: new unpinned Run uses target; old and pinned Runs remain unchanged", flush=True)
            old_boot = next(node["boot_id"] for node in console.get("/nodes", headers=admin).json() if node["node_id"] == "s10-managed-api")
            with ThreadPoolExecutor() as pool:
                future = pool.submit(chat)
                wait_until(lambda: bool(api.get("/v1/runs", params={"agent_id": "switch-test", "state": "working"}).json()["items"]))
                restart_id = command("graceful_restart", {"timeout_s": 10.0})
                wait_until(
                    lambda: any(
                        node["node_id"] == "s10-managed-api" and not node["ready"] for node in console.get("/nodes", headers=admin).json()
                    )
                )
                assert not future.done(), "in-flight request must still be draining"
                assert api.post("/v1/messages", json={"agent_id": "switch-test", "messages": [], "stream": False}).status_code == 503
                assert future.result()["state"] == "completed"
            wait_until(lambda: len(pids("api")) >= 2)
            assert pids("api")[0] != pids("api")[1]
            wait_until(lambda: api.get("/healthz").status_code == 200)
            assert receipt(restart_id)["status"] == "ok"
            assert receipt(restart_id)["result"]["boot_id"] == old_boot
            wait_until(
                lambda: any(
                    node["node_id"] == "s10-managed-api" and node["ready"] and node["boot_id"] != old_boot
                    for node in console.get("/nodes", headers=admin).json()
                )
            )
            assert chat()["model_alias"] == "model-a"
            print(
                "PASS graceful restart: admission closed, in-flight Run completed, durable ACK, new child PID, route survives restart",
                flush=True,
            )
            worker_environment = node_environment | {"NODE_ID": "s10-managed-worker"}
            launch("worker", [sys.executable, "-m", "runtime.node", "--role", "worker"], worker_environment)
            wait_until(lambda: any(node["node_id"] == "s10-managed-worker" for node in console.get("/nodes", headers=admin).json()))
            worker_restart = command("graceful_restart", {"timeout_s": 10.0}, "s10-managed-worker")
            wait_until(lambda: len(pids("worker")) >= 2)
            assert pids("worker")[0] != pids("worker")[1] and receipt(worker_restart, "s10-managed-worker")["status"] == "ok"
            print("PASS pure Worker managed restart: new PID after durable ACK", flush=True)
        print("S10.3 local node-control verification passed", flush=True)
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
        subprocess.run([docker, "rm", "-f", redis_name], capture_output=True, timeout=30)
        model.shutdown()
        model.server_close()
        for handle in handles:
            handle.close()


if __name__ == "__main__":
    main()
