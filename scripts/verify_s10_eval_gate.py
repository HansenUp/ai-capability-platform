from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx
from devkit import LANGFUSE, LF_AUTH, LF_PK, LF_SK

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    assert urlparse(LANGFUSE).hostname in {"localhost", "127.0.0.1", "::1"}, "only the existing local Langfuse is authorized"
    evidence = ROOT / "docs/evidence/s9/2026-10-18-integration.log"
    refs = re.findall(r"langfuse:[A-Za-z0-9_./@-]+", evidence.read_text())
    reference = os.environ.get("S10_EVAL_RUN_REF") or refs[-1]
    dataset, run_name = reference.removeprefix("langfuse:").split("/", 1)
    with httpx.Client(base_url=LANGFUSE, headers=LF_AUTH, timeout=15, trust_env=False) as client:
        response = client.get(f"/api/public/datasets/{quote(dataset, safe='')}/runs/{quote(run_name, safe='')}")
        response.raise_for_status()
        run = response.json()
        metadata = run["metadata"]
        agent_id, version = metadata["agent_id"], str(metadata["agent_version"])
    temporary = Path(tempfile.mkdtemp(prefix="s10-eval-"))
    os.environ.update(
        {
            "CONSOLE_DATABASE_URL": f"sqlite+aiosqlite:///{temporary}/console.db",
            "CONSOLE_ENV": "dev",
            "CONSOLE_EVAL_GATE": "langfuse",
            "EVAL_DATASETS": json.dumps({agent_id: [dataset]}),
            "LANGFUSE_HOST": LANGFUSE,
            "LANGFUSE_PUBLIC_KEY": LF_PK,
            "LANGFUSE_SECRET_KEY": LF_SK,
            "EVAL_MIN_PASS_RATE": "0.8",
            "EVAL_MIN_ITEMS": "30",
        }
    )
    from fastapi.testclient import TestClient

    from console_api import eval_gate, runtime_client
    from console_api.main import app

    runtime_calls = []

    def runtime_boundary(request):
        runtime_calls.append(request.url.path)
        return httpx.Response(200, json={"ok": True})

    runtime_client.set_transport_for_tests(httpx.MockTransport(runtime_boundary))
    headers = {"X-Console-Role": "TENANT_ADMIN", "X-Console-Tenant": "default"}
    body = {"eval_run_ref": reference, "eval_passed": True}
    try:
        with TestClient(app) as console:
            draft = {"agent_id": agent_id, "version": version, "model_alias": "model-a", "harness": "mock"}
            assert console.post("/agents", headers=headers, json=draft).status_code == 201
            publish_path = f"/agents/{agent_id}/versions/{version}/publish"
            policy = console.get(f"/agents/{agent_id}/eval-gate", headers=headers)
            assert policy.status_code == 200 and policy.json()["min_items"] == 30
            import asyncio

            async def recompute():
                async with httpx.AsyncClient(base_url=LANGFUSE, headers=LF_AUTH, timeout=15, trust_env=False) as client:
                    return await eval_gate.recompute(
                        client, run, run_name, {"agent_id": agent_id, "agent_version": version, "tenant_id": "default"}
                    )

            passed, total = asyncio.run(recompute())
            assert passed == total and total > 0, (passed, total)
            strict = console.post(publish_path, headers=headers, json=body)
            if total < 30:
                assert strict.status_code == 409 and not runtime_calls
            else:
                assert strict.status_code == 200
                runtime_calls.clear()
            eval_gate.MIN_ITEMS = total
            accepted = console.post(publish_path, headers=headers, json=body)
            assert accepted.status_code == 200, accepted.text
            assert len(runtime_calls) == 2
            count = len(runtime_calls)
            eval_gate.MIN_PASS_RATE = float("nan")
            assert console.post(publish_path, headers=headers, json=body).status_code == 409
            assert len(runtime_calls) == count
            eval_gate.MIN_PASS_RATE = 0.8
            wrong_tenant = headers | {"X-Console-Tenant": "s10-wrong-tenant"}
            assert console.post(publish_path, headers=wrong_tenant, json=body).status_code == 404
            rows = console.get("/audit", headers=headers).json()
            audit = next(row for row in reversed(rows) if row["action"] == "agent.publish")
            assert audit["detail"]["eval_gate"]["min_items"] == total
            print(f"PASS real local Langfuse read-only recomputation: {passed}/{total}, reference={reference}")
            print(
                "PASS Console gate: default sample bound, test-only configured threshold, invalid threshold blocks before Runtime boundary"
            )
            print("PASS tenant isolation and publish policy audit; Runtime management calls use an explicit MockTransport")
            print("No Langfuse writes, new persistent resources or production deployment; model/Runtime boundary not real in this check")
    finally:
        runtime_client.set_transport_for_tests(None)


if __name__ == "__main__":
    main()
