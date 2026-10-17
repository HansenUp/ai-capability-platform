"""Runtime 本地工具表（网关不可达时的回退）与能力网关种子对工单工具给出同一份入参 schema。"""

from capability_gateway.registry import SEED
from runtime.assemble.assembler import LOCAL_TOOLS


def test_runtime_local_tools_share_gateway_schema():
    gateway = {c.name: c.input_schema for c in SEED}
    for name in ("ticket.search", "ticket.get", "ticket.close_batch"):
        assert LOCAL_TOOLS[name].input_schema == gateway[name], name
