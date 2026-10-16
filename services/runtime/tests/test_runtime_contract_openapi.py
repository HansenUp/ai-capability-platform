"""Runtime 契约测试（A6 / P0-2.3）：以入库的 docs/api/runtime-openapi.json 为契约，
用 schemathesis 按 OpenAPI 生成请求做属性测试，校验服务不出 5xx、响应符合文档声明的 schema。

- 数据库 / 环境变量由 conftest.py 隔离设置，并已创建发布 demo Agent；
- 所有请求统一带开发态身份头 X-AI-Subject: human:dev；
- max_examples 较小且 derandomize，保证几秒内跑完且结果可复现。

属性测试曾发现的缺陷（cancel / reject 不存在的 Run 返回 500、since_seq 溢出返回 500）均已修复，
全部接口纳入属性测试；对应的确定性回归用例见 test_messages_and_runs.py。
"""

import json
from pathlib import Path

import schemathesis
from hypothesis import HealthCheck, settings
from schemathesis.checks import not_a_server_error
from schemathesis.specs.openapi.checks import response_schema_conformance

from runtime.main import app

SPEC = Path(__file__).resolve().parents[3] / "docs" / "api" / "runtime-openapi.json"
HEADERS = {"X-AI-Subject": "human:dev"}

schema = schemathesis.openapi.from_dict(json.loads(SPEC.read_text(encoding="utf-8")))


@schema.parametrize()
@settings(max_examples=15, deadline=None, derandomize=True, suppress_health_check=list(HealthCheck))
def test_runtime_conforms_to_openapi(case, client):
    """对每个接口生成合法请求，检查：无 5xx；文档声明过的状态码其响应体符合 schema。"""
    case.call_and_validate(app=app, headers=HEADERS, checks=[not_a_server_error, response_schema_conformance])
