"""只读现有本机 Langfuse，临时 Console 库验证，不写入观测服务。"""

import os
import re
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx
from devkit import LANGFUSE, LF_AUTH, LF_PK, LF_SK


def main() -> None:
    assert urlsplit(LANGFUSE).hostname in {"localhost", "127.0.0.1", "::1"}
    evidence = Path(__file__).resolve().parents[1] / "docs/evidence/s9/2026-10-18-integration.log"
    reference = re.findall(r"langfuse:[A-Za-z0-9_./@-]+", evidence.read_text())[-1]
    dataset, run = reference.removeprefix("langfuse:").split("/", 1)
    with httpx.Client(base_url=LANGFUSE, headers=LF_AUTH, timeout=15, trust_env=False) as client:
        result = client.get(f"/api/public/datasets/{quote(dataset, safe='')}/runs/{quote(run, safe='')}")
        result.raise_for_status()
        body = result.json()
        items = body.get("datasetRunItems")
        if not items:
            result = client.get("/api/public/dataset-run-items", params={"datasetId": body["datasetId"], "runName": run, "limit": 1})
            result.raise_for_status()
            items = result.json()["data"]
        result = client.get(f"/api/public/traces/{quote(items[0]['traceId'], safe='')}")
        result.raise_for_status()
        trace = result.json()
    metadata = trace["metadata"]["attributes"]
    tenant, agent = metadata["ai.tenant_id"], metadata["ai.agent_id"]
    stamp = datetime.fromisoformat(trace["timestamp"].replace("Z", "+00:00")).astimezone(UTC)
    with tempfile.TemporaryDirectory(prefix="s10-trace-metrics-") as directory:
        os.environ.update(
            CONSOLE_DATABASE_URL=f"sqlite+aiosqlite:///{directory}/console.db",
            CONSOLE_ENV="dev",
            LANGFUSE_HOST=LANGFUSE,
            LANGFUSE_PUBLIC_KEY=LF_PK,
            LANGFUSE_SECRET_KEY=LF_SK,
        )
        from fastapi.testclient import TestClient

        from console_api.main import app

        headers = {"X-Console-Tenant": tenant, "X-Console-Role": "TENANT_ADMIN"}
        params = {
            "agent_id": agent,
            "since": (stamp - timedelta(seconds=1)).isoformat(),
            "until": (stamp + timedelta(seconds=1)).isoformat(),
        }
        with TestClient(app) as console:
            assert console.post("/agents", headers=headers, json={"agent_id": agent, "version": "metrics-local"}).status_code == 201
            response = console.get("/metrics/traces", headers=headers, params=params)
            assert response.status_code == 200, response.text
            metrics = response.json()
            assert metrics["trace_calls"] >= 1 and metrics["run_metrics_available"] is False
            assert metrics["tokens"]["total"] is not None
            assert console.get("/metrics/traces", headers=headers | {"X-Console-Tenant": "unrelated"}, params=params).status_code == 404
            print("PASS real existing local Langfuse v3, read-only trace metrics and tenant isolation")
            print({key: metrics[key] for key in ["trace_calls", "generations", "tokens", "cost_usd", "complete_scan"]})
            print("No Langfuse writes, new persistent resources, real Runtime publication or production deployment")


if __name__ == "__main__":
    main()
