from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import jwt
import redis
from cryptography.hazmat.primitives.asymmetric import rsa
from verify_s10_node_control import ModelInfo, port, wait_until

ROOT = Path(__file__).resolve().parent.parent
IMAGE = "apache/apisix:3.11.0-debian"


def main() -> None:
    temporary = Path(tempfile.mkdtemp(prefix="s10-routing-", dir="/private/tmp"))
    print(f"evidence directory: {temporary}", flush=True)
    docker = shutil.which("docker")
    assert docker, "Docker is required; pre-pull " + IMAGE
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key())) | {"kid": "local", "alg": "RS256", "use": "sig"}
    console_port, api_a, api_b, gateway_port, redis_port = port(), port(), port(), port(), port()
    proxy_url = f"http://127.0.0.1:{gateway_port}"
    name = "s10-routing-" + uuid.uuid4().hex[:8]
    handles, processes, servers = [], [], []
    host_environment = os.environ | {"NO_PROXY": "*", "no_proxy": "*"}
    issuer = "https://s10-local-issuer.test"

    def token(tenant="routing-a", **overrides):
        claims = {
            "sub": "alice",
            "tenant": tenant,
            "iss": issuer,
            "aud": "ai-runtime",
            "iat": int(time.time()),
            "exp": int(time.time()) + 600,
        }
        return jwt.encode(claims | overrides, key, algorithm="RS256", headers={"kid": "local"})

    class Identity(BaseHTTPRequestHandler):
        requests_seen = 0

        def do_GET(self):
            Identity.requests_seen += 1
            data = json.dumps({"keys": [public]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format, *args):
            pass

    class RoutingFixture(BaseHTTPRequestHandler):
        snapshot: dict[str, Any] = {}
        status = 200
        expected_token = ""

        def do_GET(self):
            status = self.status if self.headers.get("Authorization") == "Bearer " + self.expected_token else 401
            data = json.dumps(self.snapshot).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format, *args):
            pass

    class Echo(BaseHTTPRequestHandler):
        label = ""
        healthy = True
        post_status = 200
        calls = 0

        def do_GET(self):
            status = 200 if self.healthy or self.path != "/healthz" else 503
            data = json.dumps({"node": self.label, "headers": dict(self.headers)}).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            type(self).calls += 1
            data = json.dumps({"node": self.label, "headers": {name.lower(): value for name, value in self.headers.items()}}).encode()
            self.send_response(self.post_status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format, *args):
            pass

    class PolicyFixture(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            results = []
            for item in body["requests"]:
                same_tenant = item["principal"]["tenant"] == item["resource"]["tenant"] == item["context"]["agent"]["tenant"]
                decision = "deny"
                if same_tenant:
                    decision = (
                        "require_approval"
                        if item["resource"].get("risk_level") == "write_high" and not item["context"].get("approved")
                        else "allow"
                    )
                results.append({"decision": decision, "policy_ids": ["local-routing-fixture"], "reason": "local-routing-fixture"})
            data = json.dumps({"results": results, "engine": "local-fixture", "policy_version": "s10"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format, *args):
            pass

    def server(handler):
        instance = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=instance.serve_forever, daemon=True).start()
        servers.append(instance)
        return instance

    def launch(label, command, env):
        handle = (temporary / f"{label}.log").open("w")
        handles.append(handle)
        process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=handle, stderr=subprocess.STDOUT)
        processes.append(process)
        return process

    def invoke(*args):
        result = subprocess.run([docker, *args], check=False, capture_output=True, text=True)
        if result.returncode:
            print(result.stdout, result.stderr, flush=True)
            result.check_returncode()
        return result.stdout.strip()

    try:
        redis_process = launch(
            "redis",
            [docker, "run", "--rm", "--pull", "never", "--name", name + "-redis", "-p", f"127.0.0.1:{redis_port}:6379", "redis:7-alpine"],
            host_environment,
        )

        def redis_ready():
            if redis_process.poll() is not None:
                raise RuntimeError("isolated Redis exited")
            try:
                with redis.Redis(host="127.0.0.1", port=redis_port, socket_timeout=1, socket_connect_timeout=1) as connection:
                    return connection.ping()
            except redis.RedisError:
                return False

        wait_until(redis_ready)
        identity = server(Identity)
        model = server(ModelInfo)
        policy = server(PolicyFixture)
        host_ip = invoke("run", "--rm", "--pull", "never", "--entrypoint", "getent", IMAGE, "ahostsv4", "host.docker.internal").split()[0]
        jwks_host = f"http://127.0.0.1:{identity.server_port}/jwks"
        jwks_container = f"http://host.docker.internal:{identity.server_port}/jwks"
        service_a = token(client_id="ai-routing-svc", aud="console-routing", scope="routing:read")
        service_b = token("routing-b", client_id="ai-routing-svc", aud="console-routing", scope="routing:read")
        RoutingFixture.expected_token = service_a
        routing_fixture = server(RoutingFixture)
        echo_a_type = type("EchoA", (Echo,), {"label": "echo-a"})
        echo_b_type = type("EchoB", (Echo,), {"label": "echo-b"})
        echo_a, echo_b = server(echo_a_type), server(echo_b_type)
        console_process = launch(
            "console",
            [sys.executable, "-m", "uvicorn", "console_api.main:app", "--host", "127.0.0.1", "--port", str(console_port)],
            host_environment
            | {
                "CONSOLE_DATABASE_URL": f"sqlite+aiosqlite:///{temporary}/console.db",
                "CONSOLE_ENV": "dev",
                "CONSOLE_NODE_TOKEN": "s10-local-node",
                "CONSOLE_ROUTING_JWKS_URL": jwks_host,
                "CONSOLE_ROUTING_ISSUER": issuer,
                "CONSOLE_ROUTING_AUDIENCE": "console-routing",
            },
        )
        runtime_environment = host_environment | {
            "DATABASE_URL": f"sqlite+aiosqlite:///{temporary}/runtime.db",
            "RUNTIME_ENV": "dev",
            "HARNESS": "mock",
            "AUTH_REQUIRED": "true",
            "JWKS_URL": jwks_host,
            "JWT_ISSUER": issuer,
            "JWT_AUDIENCE": "ai-runtime",
            "MODEL_GATEWAY_URL": f"http://127.0.0.1:{model.server_port}",
            "CONFIDENTIAL_MODEL_ALIASES": "model-a,model-b",
            "WORKSPACE_DIR": str(temporary / "workspaces"),
            "OTEL_SDK_DISABLED": "true",
            "NODE_ID": "",
            "RUNTIME_ROLE": "all",
            "MOCK_TURN_DELAY_S": "0.1",
            "REDIS_URL": f"redis://127.0.0.1:{redis_port}/0",
            "CREDENTIAL_ENC_KEY": uuid.uuid4().hex + uuid.uuid4().hex,
            "POLICY_URL": f"http://127.0.0.1:{policy.server_port}",
            "TOOL_DISPATCH": "local",
        }
        with httpx.Client(timeout=15, trust_env=False) as client:
            wait_until(lambda: client.get(f"http://127.0.0.1:{console_port}/healthz").status_code == 200)
            runtime_processes = []
            for label, runtime_port in (("a", api_a), ("b", api_b)):
                process = launch(
                    "runtime-" + label,
                    [sys.executable, "-m", "uvicorn", "runtime.main:app", "--host", "127.0.0.1", "--port", str(runtime_port)],
                    runtime_environment,
                )
                runtime_processes.append(process)
                wait_until(lambda runtime_port=runtime_port: client.get(f"http://127.0.0.1:{runtime_port}/healthz").status_code == 200)
            for tenant in ("routing-a", "routing-b"):
                agent_id = "routing-test" if tenant == "routing-a" else "other-test"
                admin_headers = {"Authorization": "Bearer " + token(tenant, realm_access={"roles": ["platform-admin"]})}
                response = client.post(
                    f"http://127.0.0.1:{api_a}/admin/agents",
                    headers=admin_headers,
                    json={
                        "agent_id": agent_id,
                        "tenant_id": tenant,
                        "version": "1",
                        "model_alias": "model-a",
                        "harness": "mock",
                        "config": {"tools": ["ticket.close_batch"]},
                    },
                )
                assert response.status_code == 201, response.text
                response = client.post(
                    f"http://127.0.0.1:{api_a}/admin/agents/{agent_id}/versions/1/publish",
                    headers=admin_headers,
                    params={"tenant_id": tenant},
                    json={"eval_passed": True},
                )
                assert response.status_code == 200, response.text
            console_url = f"http://127.0.0.1:{console_port}"
            node_headers = {"Authorization": "Bearer s10-local-node"}
            for label in ("a", "b"):
                response = client.post(
                    console_url + "/nodes/register", headers=node_headers, json={"node_id": label, "labels": [label, "general"]}
                )
                assert response.status_code == 200, response.text
            binding_headers = {"X-Console-Role": "PLATFORM_ADMIN", "X-Console-Tenant": "routing-a"}

            def bind(group):
                response = client.put(
                    console_url + "/agents/routing-test/bindings", headers=binding_headers, json=[{"node_group": group, "weight": 100}]
                )
                assert response.status_code == 200, response.text

            bind("a")
            plugin_dir = temporary / "lua/apisix/plugins"
            plugin_dir.mkdir(parents=True)
            plugin_dir.joinpath("ai-node-routing.lua").write_text(ROOT.joinpath("deploy/apisix/plugins/ai-node-routing.lua").read_text())
            temporary.joinpath("lua/schema.lua").write_text(ROOT.joinpath("deploy/apisix/tests/schema.lua").read_text())
            config = {
                "apisix": {"node_listen": 9080, "enable_ipv6": False, "ssl": {"enable": False}, "extra_lua_path": "/opt/ai/?.lua"},
                "deployment": {"role": "data_plane", "role_data_plane": {"config_provider": "yaml"}},
                "nginx_config": {"worker_processes": 1, "error_log_level": "warn"},
                "plugins": ["ai-node-routing"],
            }
            temporary.joinpath("config.yaml").write_text(json.dumps(config))
            common: dict[str, Any] = {
                "state_pool": "local-shared-runtime",
                "routing_url": f"http://host.docker.internal:{console_port}/internal/routing",
                "issuer": issuer,
                "audience": "ai-runtime",
                "jwks_uri": jwks_container,
                "refresh_seconds": 1,
                "stale_warning_seconds": 2,
                "nodes": {
                    label: {"host": host_ip, "port": runtime_port, "role": "all", "state_pool": "local-shared-runtime"}
                    for label, runtime_port in (("a", api_a), ("b", api_b))
                },
                "fallback_nodes": ["b"],
            }
            conf_a = common | {"tenant_id": "routing-a", "routing_token": service_a}
            conf_b = common | {"tenant_id": "routing-b", "routing_token": service_b}
            conf_policy = conf_a | {
                "routing_url": f"http://host.docker.internal:{routing_fixture.server_port}/routing",
                "fallback_nodes": ["echo-a"],
                "nodes": {
                    label: {"host": host_ip, "port": endpoint.server_port, "role": "api", "state_pool": "local-shared-runtime"}
                    for label, endpoint in (("echo-a", echo_a), ("echo-b", echo_b))
                },
            }

            def policy_snapshot(bindings, groups=None, tenant="routing-a"):
                RoutingFixture.snapshot = {
                    "tenant_id": tenant,
                    "generated_at": time.time(),
                    "default_group": "general",
                    "groups": groups if groups is not None else {"general": ["echo-a"], "a": ["echo-a"], "b": ["echo-b"]},
                    "bindings": bindings,
                }

            policy_snapshot({"routing-test": {"a": 100, "b": 0}})

            def write_routes(configs):
                routes = [
                    {"id": str(index), "uri": "/v1/*", "host": hostname, "plugins": {"ai-node-routing": configuration}}
                    for index, (hostname, configuration) in enumerate(configs, 1)
                ]
                temporary.joinpath("apisix.yaml").write_text(json.dumps({"routes": routes}) + "\n#END\n")

            write_routes([("tenant-a.test", conf_a), ("tenant-b.test", conf_b), ("policy.test", conf_policy)])
            gateway = launch(
                "apisix",
                [
                    docker,
                    "run",
                    "--rm",
                    "--pull",
                    "never",
                    "--name",
                    name,
                    "-p",
                    f"127.0.0.1:{gateway_port}:9080",
                    "-v",
                    f"{temporary}/config.yaml:/usr/local/apisix/conf/config.yaml:ro",
                    "-v",
                    f"{temporary}/apisix.yaml:/usr/local/apisix/conf/apisix.yaml:ro",
                    "-v",
                    f"{temporary}/lua:/opt/ai:ro",
                    IMAGE,
                ],
                host_environment,
            )

            def request(method, path, bearer=None, host="tenant-a.test", **kwargs):
                headers = {"Host": host, "Authorization": "Bearer " + (bearer or token())}
                headers.update(kwargs.pop("headers", {}))
                return client.request(method, proxy_url + path, headers=headers, **kwargs)

            def ready():
                if gateway.poll() is not None:
                    raise RuntimeError("APISIX exited; inspect " + str(temporary / "apisix.log"))
                return request("GET", "/v1/agents").status_code == 200

            wait_until(ready, timeout=60)
            print(
                invoke(
                    "exec",
                    "-e",
                    "LUA_CPATH=/usr/local/apisix/deps/lib/lua/5.1/?.so;;",
                    name,
                    "/usr/bin/resty",
                    "-I",
                    "/opt/ai",
                    "-I",
                    "/usr/local/apisix",
                    "-I",
                    "/usr/local/apisix/deps/share/lua/5.1",
                    "/opt/ai/schema.lua",
                ),
                flush=True,
            )
            assert Identity.requests_seen >= 2, "both Console and APISIX must fetch real JWKS"
            assert request("GET", "/v1/agents", bearer=token("routing-b")).status_code == 401
            assert request("GET", "/v1/agents", bearer=token(aud="wrong")).status_code == 401
            assert request("GET", "/v1/agents", bearer=token(iss="https://wrong-issuer.test")).status_code == 401
            assert request("GET", "/v1/agents", bearer=token(exp=1)).status_code == 401
            assert request("GET", "/v1/agents", bearer=token(exp="malformed")).status_code == 401
            assert request("GET", "/v1/agents", bearer=token(iat=int(time.time()) + 3600)).status_code == 401
            symmetric = jwt.encode({"sub": "forged"}, uuid.uuid4().hex + uuid.uuid4().hex, algorithm="HS256", headers={"kid": "local"})
            assert request("GET", "/v1/agents", bearer=symmetric).status_code == 401
            assert request("GET", "/v1/agents", headers={"Authorization": ""}).status_code == 401
            print("PASS real APISIX JWT/JWKS: signature, issuer/audience/expiry/tenant boundary", flush=True)

            body = {"agent_id": "routing-test", "messages": [{"role": "user", "content": "local route test"}], "stream": False}
            echo = request(
                "POST",
                "/v1/messages",
                host="policy.test",
                bearer=token(act={"sub": "agent:delegate"}),
                json=body,
                headers={"X-AI-Subject": "forged", "X-AI-User-Token": "forged", "X_Console_Role": "PLATFORM_ADMIN"},
            )
            assert echo.status_code == 200 and echo.json()["node"] == "echo-a", echo.text
            forwarded = echo.json()["headers"]
            assert forwarded["x-ai-subject"] == "human:alice" and forwarded["x-ai-tenant"] == "routing-a"
            assert forwarded["x-ai-actor"] == "agent:delegate"
            assert "x-ai-user-token" not in forwarded and "x_console_role" not in forwarded
            assert forwarded["authorization"].startswith("Bearer ")

            def policy_node():
                response = request("POST", "/v1/messages", host="policy.test", json=body)
                return response.json().get("node") if response.status_code == 200 else response.status_code

            policy_snapshot({"routing-test": {"a": 0, "b": 100}})
            wait_until(lambda: policy_node() == "echo-b")
            policy_snapshot({"routing-test": {"a": 100}}, tenant="routing-b")
            time.sleep(1.2)
            assert policy_node() == "echo-b"
            time.sleep(0.4)
            assert policy_node() == "echo-b"
            RoutingFixture.status = 503
            time.sleep(1.2)
            assert policy_node() == "echo-b"
            RoutingFixture.status = 200
            policy_snapshot({"routing-test": {}})
            wait_until(lambda: policy_node() == 503)
            policy_snapshot({"routing-test": {"b": 100}}, groups={"general": ["echo-a"], "b": ["unlisted-node"]})
            time.sleep(1.2)
            assert policy_node() == 503
            time.sleep(0.4)
            assert policy_node() == 503
            policy_snapshot({})
            wait_until(lambda: policy_node() == "echo-a")
            policy_snapshot({"routing-test": {"b": 100}})
            wait_until(lambda: policy_node() == "echo-b")
            echo_b_type.healthy = False
            time.sleep(1.2)
            assert policy_node() == 503
            echo_b_type.healthy = True
            time.sleep(1.2)
            assert policy_node() == "echo-b"
            echo_b_type.post_status = 500
            count = echo_b_type.calls
            assert request("POST", "/v1/messages", host="policy.test", json=body).status_code == 500
            assert echo_b_type.calls == count + 1, "side-effecting POST must not be retried"
            echo_b_type.post_status = 200
            assert request("POST", "/v1/admin", host="policy.test", json=body).status_code == 403
            print(
                "PASS weights/zero/default, whitelist, identity stripping, invalid-cache isolation, health closure, no POST retry",
                flush=True,
            )
            response = request(
                "POST",
                "/v1/messages",
                json=body,
                headers={"X-AI-Tenant": "forged", "X-AI-Subject": "human:evil", "X-Console-Role": "PLATFORM_ADMIN"},
            )
            assert response.status_code == 200, response.text
            run_id = response.json()["run_id"]
            view = request("GET", f"/v1/runs/{run_id}")
            assert view.status_code == 200, view.text
            assert view.json()["model_alias"] == "model-a", view.text
            assert request("GET", f"/v1/runs/{run_id}", bearer=token("routing-b"), host="tenant-b.test").status_code in (403, 404)
            assert request("GET", f"/v1/runs/{run_id}/events").status_code == 200
            stream = request("GET", f"/v1/runs/{run_id}/stream")
            assert stream.status_code == 200 and "text/event-stream" in stream.headers.get("content-type", ""), stream.text
            pending = request(
                "POST",
                "/v1/messages",
                json=body
                | {
                    "stream": True,
                    "messages": [{"role": "user", "content": '!tool ticket.close_batch {"ids":["LOCAL-1"]}'}],
                },
            )
            assert pending.status_code == 200, pending.text
            pending_id = pending.headers["x-run-id"]
            frames = [json.loads(line[5:]) for line in pending.text.splitlines() if line.startswith("data:")]
            card = next(frame for frame in frames if frame.get("type") == "card")
            approval = {key: card["payload"][key] for key in ("call_id", "params_hash")}
            assert request("GET", f"/v1/runs/{pending_id}").json()["state"] == "auth-required"
            bind("b")
            time.sleep(1.2)
            request("GET", "/v1/agents")
            time.sleep(0.4)
            runtime_processes[0].terminate()
            runtime_processes[0].wait(timeout=15)
            response = request("GET", f"/v1/runs/{run_id}")
            wait_until(lambda: request("GET", f"/v1/runs/{run_id}").status_code == 200)
            assert request("GET", f"/v1/runs/{run_id}/events").status_code == 200
            stream = request("GET", f"/v1/runs/{run_id}/stream")
            assert stream.status_code == 200 and "text/event-stream" in stream.headers.get("content-type", ""), stream.text
            assert request("POST", f"/v1/runs/{run_id}/resume", json={}).status_code == 409
            print("PASS existing Run/SSE after Agent rebind and original HTTP node shutdown (shared durable DB)", flush=True)

            assert request("POST", f"/v1/runs/{pending_id}/approve", json=approval).status_code == 403
            assert request("POST", f"/v1/runs/{pending_id}/approve", bearer=token(client_id="local-app"), json=approval).status_code == 403
            approved = request("POST", f"/v1/runs/{pending_id}/approve", bearer=token(sub="bob"), json=approval)
            assert approved.status_code == 200, approved.text
            wait_until(lambda: request("GET", f"/v1/runs/{pending_id}").json().get("state") == "completed")
            resumed = request("POST", f"/v1/runs/{pending_id}/resume", json={})
            assert resumed.status_code == 200 and "text/event-stream" in resumed.headers.get("content-type", ""), resumed.text
            events = request("GET", f"/v1/runs/{pending_id}/events").json()
            assert sum(event["type"] == "tool.executed" for event in events) == 1
            print(
                "PASS suspended mock-write approval/resume on surviving node; self/application approval denied; execution exactly once",
                flush=True,
            )

            durable = request("POST", "/v1/runs", json={"agent_id": "routing-test", "input": body["messages"]})
            assert durable.status_code == 202, durable.text
            durable_id = durable.json()["run_id"]
            wait_until(lambda: request("GET", f"/v1/runs/{durable_id}").json().get("state") == "completed")
            stream = request("GET", f"/v1/runs/{durable_id}/stream")
            assert stream.status_code == 200 and "text/event-stream" in stream.headers.get("content-type", ""), stream.text
            print("PASS durable Run queued through shared Redis, executed by surviving Worker and tailed via gateway SSE", flush=True)

            time.sleep(1.2)
            request("GET", "/v1/agents")
            time.sleep(0.5)
            console_process.terminate()
            console_process.wait(timeout=10)
            assert request("POST", "/v1/messages", json=body).status_code == 200
            time.sleep(1.2)
            assert request("POST", "/v1/messages", json=body).status_code == 200
            print("PASS Console outage preserves last valid tenant cache", flush=True)
            conf_cold = conf_a | {
                "routing_token": token(client_id="ai-routing-svc", aud="console-routing", scope="routing:read", sub="cold-start")
            }
            conf_empty = conf_cold | {"fallback_nodes": []}
            write_routes(
                [
                    ("tenant-a.test", conf_a),
                    ("tenant-b.test", conf_b),
                    ("policy.test", conf_policy),
                    ("cold.test", conf_cold),
                    ("empty.test", conf_empty),
                ]
            )
            wait_until(lambda: request("GET", "/v1/agents", host="cold.test").status_code == 200)
            wait_until(lambda: request("GET", "/v1/agents", host="empty.test").status_code == 503)
            print("PASS cold start uses explicit tenant fallback; missing fallback fails closed", flush=True)
            invoke("logs", name)
            print("S10.4 real local APISIX verification passed", flush=True)
    finally:
        subprocess.run([docker, "rm", "-f", name], capture_output=True, check=False)
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        subprocess.run([docker, "rm", "-f", name + "-redis"], capture_output=True, check=False)
        for handle in handles:
            handle.close()
        for instance in servers:
            instance.shutdown()
            instance.server_close()


if __name__ == "__main__":
    main()
