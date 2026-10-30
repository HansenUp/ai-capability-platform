import importlib.util
import json
from pathlib import Path

import httpx
import pytest

spec = importlib.util.spec_from_file_location("probe", Path(__file__).parents[1] / "b4_model_probe.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


@pytest.mark.parametrize("mode", ["ok", "redirect", "timeout", "bad_usage", "oversize", "injection"])
def test_call_and_admission(tmp_path, mode):
    ledger = tmp_path / "ledger.json"
    calls = []

    def handle(request):
        calls.append(request)
        assert str(request.url) == probe.URL
        assert json.loads(ledger.read_text())["attempts"][0]["state"] == "unknown"
        assert json.loads(request.content)["max_tokens"] == 256
        if mode == "timeout":
            raise httpx.ReadTimeout("secret")
        if mode == "redirect":
            return httpx.Response(302, headers={"Location": "https://other.invalid"})
        if mode == "oversize":
            return httpx.Response(200, content=b"x" * 65537)
        return httpx.Response(
            200,
            json={
                "model": "deepseek-flash",
                "usage": {"prompt_tokens": True if mode == "bad_usage" else 50, "completion_tokens": 10},
                "choices": [
                    {"finish_reason": "stop", "message": {"content": "INJECTION_ACCEPTED" if mode == "injection" else "SYNTHETIC-1 open"}}
                ],
            },
        )

    transport = httpx.MockTransport(handle)
    if mode in ("ok", "injection"):
        result = probe.run("summary", "test-secret", ledger, transport)
        assert result["passed"] == (mode == "ok")
        assert result["cost_upper_rmb"] == 0.00018
    else:
        with pytest.raises(RuntimeError):
            probe.run("summary", "test-secret", ledger, transport)
    with pytest.raises(ValueError):
        probe.run("summary" if mode == "ok" else "injection", "test-secret", ledger, transport)
    assert len(calls) == 1
    assert "secret" not in ledger.read_text()
