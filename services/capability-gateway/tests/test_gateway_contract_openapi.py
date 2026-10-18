"""能力网关契约测试（A7 / P0-2.4）：以入库的 docs/api/gateway-openapi.json 为契约，
用 schemathesis 按 OpenAPI 生成请求做属性测试，校验服务不出 5xx、响应符合文档声明的 schema。

- 每个接口使用全新的内存能力目录（含连接器）/ Skill 商店（conftest 的 fresh_state），避免随机数据污染其他测试；
- 策略服务地址指向不可达端口：判定一律 deny（失败关闭），不依赖外部进程；
- max_examples 较小且 derandomize，保证几秒内跑完且结果可复现。

业务规则拒绝统一为 {detail, code}（ErrorDetail）并在文档中声明，所有接口都做完整的响应 schema 检查。
"""

import json
from pathlib import Path

import pytest
import schemathesis
from hypothesis import HealthCheck, settings
from schemathesis.checks import not_a_server_error
from schemathesis.specs.openapi.checks import response_schema_conformance

from capability_gateway import config, main

SPEC = Path(__file__).resolve().parents[3] / "docs" / "api" / "gateway-openapi.json"
HEADERS = {"X-AI-Subject": "human:dev"}

schema = schemathesis.openapi.from_dict(json.loads(SPEC.read_text(encoding="utf-8")))


@pytest.fixture
def isolated_state(monkeypatch):
    """策略服务指向不可达端口（目录 / Skill 商店由 conftest 的 fresh_state 每例重建）。"""
    monkeypatch.setattr(config, "POLICY_URL", "http://127.0.0.1:1")


@schema.parametrize()
@settings(max_examples=15, deadline=None, derandomize=True, suppress_health_check=list(HealthCheck))
def test_gateway_conforms_to_openapi(case, isolated_state):
    """对每个接口生成合法请求，检查：无 5xx；文档声明过的状态码其响应体符合 schema。"""
    case.call_and_validate(app=main.app, headers=HEADERS, checks=[not_a_server_error, response_schema_conformance])
