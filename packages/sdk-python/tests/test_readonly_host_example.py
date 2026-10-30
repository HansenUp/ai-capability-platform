import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("readonly_host", Path(__file__).parents[1] / "examples/readonly_host.py")
assert spec and spec.loader
example = importlib.util.module_from_spec(spec)
spec.loader.exec_module(example)


@pytest.mark.parametrize("scenario", example.SCENARIOS)
async def test_synthetic_host_is_readonly_and_never_infers_completion(scenario):
    result = await example.run_demo(scenario)
    assert result["clients_closed"] is True
    assert result["writes_enabled"] is False
    assert result["run_completion"] == "unknown"
    assert result["requests"] == ["/v1/workbench/identity", "/v1/workbench/attempts", "/v1/runs/demo-run/stream"]
    assert result["pending_write"] == ("PREPARED" if scenario == "locked" else None)
    if scenario in ("disconnect", "foreign"):
        assert result["events"] == []
        assert "no automatic reconnect" in result["read_status"]
    else:
        assert result["events"] == [{"seq": 1, "type": "run.state_changed"}]
        assert "no Run completion or unlock" in result["read_status"]


async def test_unknown_scenario_never_starts_client():
    with pytest.raises(ValueError, match="Unknown synthetic scenario"):
        await example.run_demo("real")
