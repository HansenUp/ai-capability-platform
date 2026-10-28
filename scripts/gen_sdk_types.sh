#!/usr/bin/env bash
# 由 docs/api 下的 OpenAPI 生成 SDK 类型骨架（A8 / 实施计划 P0-2.5）。
#   Python：datamodel-code-generator → packages/sdk-python/src/ai_platform_sdk/types_{runtime,gateway}.py（pydantic v2）
#   Web   ：openapi-typescript       → packages/sdk-web/src/types/runtime.d.ts
# 前置：make setup（安装 sdk-python[dev]）与 pnpm install。OpenAPI 变更后先 make openapi，再运行本脚本。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${VENV:-.venv}"
case "$VENV" in /*) ;; *) VENV="$ROOT/$VENV" ;; esac
PY_OUT="$ROOT/packages/sdk-python/src/ai_platform_sdk"

for svc in runtime gateway; do
  "$VENV/bin/datamodel-codegen" \
    --input "$ROOT/docs/api/${svc}-openapi.json" \
    --input-file-type openapi \
    --output "$PY_OUT/types_${svc}.py" \
    --output-model-type pydantic_v2.BaseModel \
    --target-python-version 3.11 \
    --use-union-operator \
    --use-standard-collections \
    --use-double-quotes \
    --use-schema-description \
    --formatters ruff-check ruff-format \
    --output-datetime-class datetime \
    --disable-timestamp \
    --custom-file-header "# 由 scripts/gen_sdk_types.sh 根据 docs/api/${svc}-openapi.json 自动生成，请勿手改。"
  "$VENV/bin/python" "$ROOT/scripts/normalize_sdk_types.py" "$PY_OUT/types_${svc}.py"
done

# Web SDK 的生成命令定义在 packages/sdk-web/package.json 的 gen:types 脚本中
(cd "$ROOT" && pnpm --filter @ai-platform/sdk-web run gen:types)
