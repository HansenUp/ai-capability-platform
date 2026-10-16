"""导出 Runtime 与能力网关的 OpenAPI 文档到 docs/api/（A6 / A7，实施计划 P0-2.3、P0-2.4）。

- Runtime（runtime.main:app）→ docs/api/runtime-openapi.json
- 能力网关（capability_gateway.main:app）→ docs/api/gateway-openapi.json

约定：
- info.version 固定为 "v1"（接口冻结版本），与包版本号解耦；
- JSON 统一 indent=2、ensure_ascii=False、键排序、末尾换行，保证重复生成结果逐字节一致、diff 稳定；
- Runtime 的 /v1/messages 与 /resume 以 SSE 返回统一事件流，FastAPI 无法自动推导其 schema，
  因此把契约包 ai_contract 中的 Event（判别联合）、RunEvent、RunState 追加进 components.schemas，
  并在根级扩展字段 x-sse-event-schema 中指明 SSE data 帧的 schema，供 SDK 类型生成使用。

用法：
    .venv/bin/python scripts/export_openapi.py                 # 写入 docs/api/
    .venv/bin/python scripts/export_openapi.py --out-dir DIR   # 写入指定目录（make openapi-check 使用）
"""

from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
API_VERSION = "v1"  # 冻结的接口版本号

# 导入服务前给出开发态默认值：mock Harness、内存 SQLite，避免导出时在仓库内落库文件。
# 仅在调用方未显式设置时生效；这些值不影响 OpenAPI 内容。
os.environ.setdefault("HARNESS", "mock")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")


def _contract_schemas() -> dict[str, Any]:
    """把契约包中 SSE / 事件日志相关类型转成 OpenAPI components.schemas 片段（引用指向 components）。"""
    from pydantic import TypeAdapter

    from ai_contract import Event, RunEvent, RunState

    ref_template = "#/components/schemas/{model}"
    out: dict[str, Any] = {}
    for name, tp in (("Event", Event), ("RunEvent", RunEvent), ("RunState", RunState)):
        schema = TypeAdapter(tp).json_schema(ref_template=ref_template, mode="serialization")
        out.update(schema.pop("$defs", {}))
        schema.setdefault("title", name)
        out[name] = schema
    return out


def _merge_components(spec: dict[str, Any], extra: dict[str, Any]) -> None:
    """合并额外 schema；与 FastAPI 自动生成的同名 schema 内容不一致时直接报错，防止静默覆盖。"""
    schemas = spec.setdefault("components", {}).setdefault("schemas", {})
    for name, schema in extra.items():
        if name in schemas and schemas[name] != schema:
            raise SystemExit(f"components.schemas.{name} 与契约类型同名但内容不同，请重命名其一")
        schemas[name] = schema


def build_runtime_spec() -> dict[str, Any]:
    """生成 Runtime 的 OpenAPI 文档（含契约事件 schema）。"""
    from runtime.main import app

    spec = copy.deepcopy(app.openapi())  # app.openapi() 有缓存，复制后再修改
    spec["info"]["version"] = API_VERSION
    _merge_components(spec, _contract_schemas())
    spec["x-sse-event-schema"] = "#/components/schemas/Event"
    return spec


def build_gateway_spec() -> dict[str, Any]:
    """生成能力网关的 OpenAPI 文档。"""
    from capability_gateway.main import app

    spec = copy.deepcopy(app.openapi())
    spec["info"]["version"] = API_VERSION
    return spec


def dumps(spec: dict[str, Any]) -> str:
    """稳定序列化：键排序、缩进 2、保留中文、末尾换行。"""
    return json.dumps(spec, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="导出 Runtime 与能力网关的 OpenAPI 文档")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "docs" / "api", help="输出目录，默认 docs/api")
    args = parser.parse_args(argv)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    for filename, build in (("runtime-openapi.json", build_runtime_spec), ("gateway-openapi.json", build_gateway_spec)):
        path = args.out_dir / filename
        path.write_text(dumps(build()), encoding="utf-8")
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
