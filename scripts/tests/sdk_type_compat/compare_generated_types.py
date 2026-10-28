#!/usr/bin/env python3
"""离线对照 Python SDK 生成类型的候选。

只读仓库里的 OpenAPI 与现有生成文件，把候选写到 --out。
不修改 packages/sdk-python，不安装依赖，不连接外部服务。
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
import subprocess
import sys
import traceback
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ValidationError
from pydantic_core import PydanticUndefined

ROOT = Path(__file__).resolve().parents[3]
VENV_PY = ROOT / ".venv" / "bin" / "python"
CODEGEN = ROOT / ".venv" / "bin" / "datamodel-codegen"
RUFF = ROOT / ".venv" / "bin" / "ruff"
MYPY = ROOT / ".venv" / "bin" / "mypy"
COMMITTED = {
    "runtime": ROOT / "packages/sdk-python/src/ai_platform_sdk/types_runtime.py",
    "gateway": ROOT / "packages/sdk-python/src/ai_platform_sdk/types_gateway.py",
}
OPENAPI = {
    "runtime": ROOT / "docs/api/runtime-openapi.json",
    "gateway": ROOT / "docs/api/gateway-openapi.json",
}
BASE_ARGS = [
    "--input-file-type",
    "openapi",
    "--output-model-type",
    "pydantic_v2.BaseModel",
    "--target-python-version",
    "3.11",
    "--use-union-operator",
    "--use-standard-collections",
    "--use-double-quotes",
    "--use-schema-description",
    "--formatters",
    "ruff-check",
    "ruff-format",
    "--output-datetime-class",
    "datetime",
    "--disable-timestamp",
]
GENERATOR_CANDIDATES: list[tuple[str, list[str]]] = [
    ("baseline", []),
    ("annotated", ["--use-annotated"]),
    ("field-constraints", ["--field-constraints"]),
    ("enum-default", ["--deserialize-default-values", "enum"]),
    ("annotated-enum-default", ["--use-annotated", "--deserialize-default-values", "enum"]),
    ("annotated-literal-enum", ["--use-annotated", "--enum-field-as-literal", "all"]),
]
DATETIME_SAMPLES = [
    "2026-10-16T13:40:08.136711",
    "2026-10-16T13:40:08.2+00:00",
    "2026-10-27T02:00:00Z",
    "2026-10-27",
    "not-a-date",
    1698393600,
]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(
    cmd: list[str], cwd: Path, env: dict[str, str] | None = None, input_bytes: bytes | None = None
) -> subprocess.CompletedProcess[bytes]:
    merged = None
    if env is not None:
        import os

        merged = os.environ.copy()
        merged.update(env)
    return subprocess.run(cmd, cwd=cwd, env=merged, input=input_bytes, capture_output=True, check=False)  # noqa: S603


def generate(service: str, extra: list[str], dest: Path) -> dict[str, Any]:
    header = f"# 由 scripts/gen_sdk_types.sh 根据 docs/api/{service}-openapi.json 自动生成，请勿手改。"
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(CODEGEN),
        "--input",
        str(OPENAPI[service]),
        *BASE_ARGS[:2],
        "--output",
        str(dest),
        *BASE_ARGS[2:],
        "--custom-file-header",
        header,
        *extra,
    ]
    proc = run(cmd, ROOT)
    return {
        "command": cmd,
        "exit": proc.returncode,
        "stderr": proc.stderr.decode(errors="replace")[-4000:],
        "exists": dest.exists(),
    }


def ruff_stabilize(path: Path) -> None:
    """用与仓库包装器相同的 stdin 方式整理转换结果。文件名故意不匹配 types_*.py。"""
    data = path.read_bytes()
    fake = "scripts/tests/sdk_type_compat/candidate_probe.py"
    checked = run(
        [str(RUFF), "check", "--fix", "--force-exclude", "--stdin-filename", fake, "-"],
        ROOT,
        input_bytes=data,
    )
    if checked.stdout:
        data = checked.stdout
    formatted = run(
        [str(RUFF), "format", "--force-exclude", "--stdin-filename", fake, "-"],
        ROOT,
        input_bytes=data,
    )
    if formatted.returncode != 0 or not formatted.stdout:
        raise RuntimeError(formatted.stderr.decode(errors="replace")[:2000])
    path.write_bytes(formatted.stdout)


def _enum_maps(tree: ast.AST) -> dict[str, dict[Any, str]]:
    found: dict[str, dict[Any, str]] = {}
    for node in tree.body if isinstance(tree, ast.Module) else []:
        if not isinstance(node, ast.ClassDef):
            continue
        base_names = {base.id for base in node.bases if isinstance(base, ast.Name)}
        if not base_names & {"StrEnum", "IntEnum", "Enum"}:
            continue
        mapping: dict[Any, str] = {}
        duplicated: set[Any] = set()
        for stmt in node.body:
            if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
                if isinstance(stmt.value, ast.Constant):
                    if stmt.value.value in mapping:
                        duplicated.add(stmt.value.value)
                    mapping[stmt.value.value] = stmt.targets[0].id
        for key in duplicated:
            mapping.pop(key, None)
        found[node.name] = mapping
    return found


def _enum_in_annotation(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        names = [item for item in (_enum_in_annotation(node.left), _enum_in_annotation(node.right)) if item]
        if len(names) == 1:
            return names[0]
    return None


class _SyntaxRewrite(ast.NodeTransformer):
    def __init__(self, enums: dict[str, dict[Any, str]], rewrite_enum_default: bool) -> None:
        self.enums = enums
        self.rewrite_enum_default = rewrite_enum_default
        self.used_string_constraints = False

    def visit_Call(self, node: ast.Call) -> ast.AST:
        self.generic_visit(node)
        if not isinstance(node.func, ast.Name) or node.func.id not in {"constr", "conint", "confloat"}:
            return node
        if node.args:
            raise ValueError(f"{node.func.id} 使用了位置参数，转换器拒绝猜测")
        if node.func.id == "constr":
            self.used_string_constraints = True
            metadata: ast.expr = ast.Call(func=ast.Name("StringConstraints", ast.Load()), args=[], keywords=node.keywords)
            base: ast.expr = ast.Name("str", ast.Load())
        else:
            metadata = ast.Call(func=ast.Name("Field", ast.Load()), args=[], keywords=node.keywords)
            base = ast.Name("int" if node.func.id == "conint" else "float", ast.Load())
        return ast.Subscript(
            value=ast.Name("Annotated", ast.Load()),
            slice=ast.Tuple(elts=[base, metadata], ctx=ast.Load()),
            ctx=ast.Load(),
        )

    def visit_AnnAssign(self, node: ast.AnnAssign) -> ast.AST:
        self.generic_visit(node)
        if not self.rewrite_enum_default or not isinstance(node.value, ast.Call):
            return node
        if not isinstance(node.value.func, ast.Name) or node.value.func.id != "Field" or not node.value.args:
            return node
        enum_name = _enum_in_annotation(node.annotation)
        mapping = self.enums.get(enum_name or "")
        first = node.value.args[0]
        if mapping is None or not isinstance(first, ast.Constant) or not isinstance(first.value, str):
            return node
        member = mapping.get(first.value)
        if member is None:
            return node
        node.value.args[0] = ast.Attribute(value=ast.Name(enum_name, ast.Load()), attr=member, ctx=ast.Load())
        return node


def _names_used(tree: ast.AST) -> set[str]:
    found: set[str] = set()

    class Visitor(ast.NodeVisitor):
        def visit_Name(self, node: ast.Name) -> None:
            found.add(node.id)

    Visitor().visit(tree)
    return found


def rewrite_syntax(source: Path, dest: Path, rewrite_enum_default: bool) -> None:
    tree = ast.parse(source.read_text())
    rewriter = _SyntaxRewrite(_enum_maps(tree), rewrite_enum_default)
    tree = rewriter.visit(tree)
    used = _names_used(tree)
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "typing" and "Annotated" in used:
            if not any(alias.name == "Annotated" for alias in node.names):
                node.names.insert(0, ast.alias(name="Annotated"))
        if isinstance(node, ast.ImportFrom) and node.module == "pydantic":
            node.names = [alias for alias in node.names if alias.name in used]
            if rewriter.used_string_constraints and not any(alias.name == "StringConstraints" for alias in node.names):
                node.names.append(ast.alias(name="StringConstraints"))
    ast.fix_missing_locations(tree)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(ast.unparse(tree) + "\n")
    compile(dest.read_text(), str(dest), "exec")
    ruff_stabilize(dest)


def mypy_text(paths: list[Path], cache: Path) -> dict[str, Any]:
    cache.mkdir(parents=True, exist_ok=True)
    proc = run(
        [str(MYPY), "--cache-dir", str(cache), "--no-error-summary", *[str(path) for path in paths]],
        ROOT,
        env={"MYPY_CACHE_DIR": str(cache)},
    )
    text = proc.stdout.decode(errors="replace")
    errors = [line for line in text.splitlines() if ": error:" in line]
    codes: dict[str, int] = {}
    files: dict[str, int] = {}
    for line in errors:
        files[line.split(":", 1)[0]] = files.get(line.split(":", 1)[0], 0) + 1
        code = line.rsplit("[", 1)[-1][:-1] if line.endswith("]") else "NOCODE"
        codes[code] = codes.get(code, 0) + 1
    return {"exit": proc.returncode, "errors": len(errors), "codes": codes, "files": files, "text": text}


def classify_mypy(text: str, sources: dict[str, Path]) -> list[dict[str, Any]]:
    classes: dict[str, list[tuple[int, str]]] = {}
    for label, path in sources.items():
        current = "<module>"
        rows: list[tuple[int, str]] = []
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("class "):
                current = stripped.split("(", 1)[0].removeprefix("class ").strip()
            rows.append((number, current))
        classes[label] = rows
    classified = []
    for line in text.splitlines():
        if ": error:" not in line:
            continue
        location, _, message = line.partition(": error: ")
        path_text, line_no, *_ = location.split(":")
        label = "runtime" if path_text.endswith("types_runtime.py") else "gateway" if path_text.endswith("types_gateway.py") else path_text
        number = int(line_no)
        current = classes.get(label, [(-1, "<unknown>")])[-1][1]
        for start, name in classes.get(label, []):
            if start <= number:
                current = name
        code = message.rsplit("[", 1)[-1][:-1] if message.endswith("]") else "NOCODE"
        source_line = ""
        source_path = sources.get(label)
        if source_path is not None:
            source_rows = source_path.read_text().splitlines()
            if 1 <= number <= len(source_rows):
                source_line = source_rows[number - 1].strip()
        kind = "other"
        if code == "valid-type" and "constr(" in source_line or "conint(" in source_line or "confloat(" in source_line:
            kind = "constrained-type"
        elif code == "misc" and "RootModel" in source_line:
            kind = "root-model"
        elif code == "assignment" and 'has type "str"' in message:
            kind = "enum-default"
        classified.append(
            {"file": label, "line": number, "class": current, "code": code, "kind": kind, "source": source_line, "message": message}
        )
    return classified


def load_module(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载 {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def describe(value: Any) -> Any:
    if isinstance(value, Enum):
        return {"__t": "enum", "cls": type(value).__name__, "name": value.name, "value": value.value, "str": isinstance(value, str)}
    if isinstance(value, datetime):
        return {"__t": "datetime", "iso": value.isoformat(), "aware": value.tzinfo is not None}
    if isinstance(value, UUID):
        return {"__t": "uuid", "value": str(value)}
    if isinstance(value, dict):
        return {str(key): describe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [describe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return {"__t": type(value).__name__, "repr": repr(value)}


def error_view(exc: ValidationError) -> list[dict[str, Any]]:
    rows = []
    for item in exc.errors():
        ctx = {}
        for key, value in (item.get("ctx") or {}).items():
            ctx[key] = value if isinstance(value, (str, int, float, bool)) or value is None else repr(value)
        rows.append({"type": item["type"], "loc": list(item["loc"]), "ctx": ctx})
    return rows


def outcome(model: type[BaseModel], payload: Any, *, construct: bool = False) -> dict[str, Any]:
    try:
        if construct:
            if not isinstance(payload, dict):
                obj = model.model_validate(payload)
            else:
                kwargs = {}
                for name, field in model.model_fields.items():
                    key = field.alias or name
                    if key in payload:
                        kwargs[name] = payload[key]
                    elif name in payload:
                        kwargs[name] = payload[name]
                obj = model.model_construct(**kwargs)
        else:
            obj = model.model_validate(payload)
    except ValidationError as exc:
        return {"status": "reject", "errors": error_view(exc)}
    except Exception as exc:  # noqa: BLE001 —— 对照器要留下候选加载后的真实异常
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
    attrs: dict[str, Any] = {}
    for name in model.model_fields:
        if not hasattr(obj, name):
            continue
        value = getattr(obj, name)
        if isinstance(value, list):
            attrs[name] = [type(item).__name__ for item in value[:2]]
        else:
            attrs[name] = type(value).__name__
    identity = None
    if model.__name__ == "RunEvent" and hasattr(obj, "type"):
        enum_cls = getattr(sys.modules[model.__module__], "RunEventType", None)
        member = getattr(enum_cls, "run_done", None)
        identity = bool(member is not None and obj.type is member)
    return {
        "status": "accept",
        "python": describe(obj.model_dump(mode="python")),
        "json": describe(json.loads(obj.model_dump_json())),
        "alias": describe(json.loads(obj.model_dump_json(by_alias=True))),
        "attrs": attrs,
        "run_done_identity": identity,
    }


def resolve_schema(schema: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        return root.get("$defs", {}).get(name, schema)
    return schema


def minimal(schema: dict[str, Any], root: dict[str, Any], depth: int = 0) -> Any:
    if depth > 8:
        return None
    if "$ref" in schema:
        return minimal(resolve_schema(schema, root), root, depth + 1)
    if "allOf" in schema:
        merged: dict[str, Any] = {}
        for part in schema["allOf"]:
            value = minimal(part, root, depth + 1)
            if isinstance(value, dict):
                merged.update(value)
        return merged
    options = schema.get("anyOf") or schema.get("oneOf")
    if options:
        chosen = next((item for item in options if item.get("type") != "null"), options[0])
        return minimal(chosen, root, depth + 1)
    if "const" in schema:
        return schema["const"]
    if "enum" in schema:
        return schema["enum"][0]
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((item for item in kind if item != "null"), kind[0])
    if kind == "string":
        if schema.get("format") == "date-time":
            return "2026-10-16T13:40:08.136711"
        if schema.get("format") == "uuid":
            return "00000000-0000-4000-8000-000000000001"
        low = int(schema.get("minLength", 0))
        high = schema.get("maxLength")
        size = low or (0 if high == 0 else 1)
        if high is not None:
            size = min(size, int(high))
        return "a" * size
    if kind == "integer":
        return int(schema.get("minimum", 0))
    if kind == "number":
        return float(schema.get("minimum", 0))
    if kind == "boolean":
        return False
    if kind == "array":
        count = int(schema.get("minItems", 0))
        return [minimal(schema.get("items", {}), root, depth + 1) for _ in range(count)]
    if kind == "object" or "properties" in schema:
        required = set(schema.get("required") or [])
        return {key: minimal(value, root, depth + 1) for key, value in (schema.get("properties") or {}).items() if key in required}
    if kind == "null":
        return None
    return None


def unwrap(schema: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    schema = resolve_schema(schema, root)
    options = schema.get("anyOf") or schema.get("oneOf")
    if options:
        chosen = next((item for item in options if item.get("type") != "null"), None)
        if chosen is not None:
            return unwrap(chosen, root)
    return schema


def boundary_values(schema: dict[str, Any], root: dict[str, Any]) -> list[Any]:
    schema = unwrap(schema, root)
    values: list[Any] = [None]
    if "enum" in schema:
        values.extend(schema["enum"])
        values.append("__not_a_member__")
    if "const" in schema:
        values.append(schema["const"])
    kind = schema.get("type")
    if kind == "string":
        low = int(schema.get("minLength", 0))
        high = schema.get("maxLength")
        values.extend(["", "a" * max(low - 1, 0), "a" * low])
        if high is not None:
            values.extend(["a" * int(high), "a" * (int(high) + 1)])
        if schema.get("format") == "date-time":
            values.extend(DATETIME_SAMPLES)
        if schema.get("format") == "uuid":
            values.extend(["00000000-0000-4000-8000-000000000001", "not-a-uuid"])
        values.append(1)
    elif kind == "integer":
        low = schema.get("minimum")
        high = schema.get("maximum")
        values.extend([0, 1, -1, "1", True])
        if low is not None:
            values.extend([int(low) - 1, int(low)])
        if high is not None:
            values.extend([int(high), int(high) + 1])
    elif kind == "number":
        low = schema.get("minimum")
        high = schema.get("maximum")
        values.extend([0, 0.0, 1, "0.5", True])
        if low is not None:
            values.extend([float(low) - 0.1, float(low)])
        if high is not None:
            values.extend([float(high), float(high) + 0.1])
    elif kind == "array":
        item = minimal(schema.get("items", {}), root)
        high = schema.get("maxItems")
        values.extend([[], [item] if item is not None else []])
        if high is not None:
            values.append([item] * int(high))
            values.append([item] * (int(high) + 1))
        else:
            values.append([item] * 33)
    elif kind == "boolean":
        values.extend([True, False, "true", 1, 0])
    elif kind == "object":
        values.append(minimal(schema, root))
        values.append("not-an-object")
    return values


def json_diff(left: Any, right: Any, path: str = "$") -> list[dict[str, Any]]:
    if type(left) is not type(right):
        return [{"path": path, "left": left, "right": right}]
    if isinstance(left, dict):
        rows: list[dict[str, Any]] = []
        for key in sorted(set(left) | set(right)):
            if key not in left or key not in right:
                rows.append({"path": f"{path}.{key}", "left": left.get(key, "<missing>"), "right": right.get(key, "<missing>")})
            else:
                rows.extend(json_diff(left[key], right[key], f"{path}.{key}"))
        return rows
    if isinstance(left, list):
        return [] if left == right else [{"path": path, "left": left, "right": right}]
    if left != right:
        return [{"path": path, "left": left, "right": right}]
    return []


def field_snapshot(model: type[BaseModel]) -> dict[str, Any]:
    rows = {}
    for name, field in model.model_fields.items():
        default: Any = {"__t": "undefined"}
        if field.default is not PydanticUndefined:
            default = describe(field.default)
        rows[name] = {
            "required": field.is_required(),
            "default": default,
            "default_factory": field.default_factory is not None,
            "alias": field.alias,
            "validation_alias": repr(field.validation_alias),
            "serialization_alias": field.serialization_alias,
        }
    return rows


def module_models(module: Any) -> dict[str, type[BaseModel]]:
    return {
        name: value
        for name, value in vars(module).items()
        if isinstance(value, type) and issubclass(value, BaseModel) and value.__module__ == module.__name__
    }


def module_enums(module: Any) -> dict[str, list[list[Any]]]:
    return {
        name: [[member.name, member.value] for member in value]
        for name, value in vars(module).items()
        if isinstance(value, type) and issubclass(value, Enum) and value.__module__ == module.__name__
    }


def compare_pair(base_mod: Any, cand_mod: Any, label: str) -> dict[str, Any]:
    base_models = module_models(base_mod)
    cand_models = module_models(cand_mod)
    diffs: list[dict[str, Any]] = []
    equal = 0
    skipped = 0
    enum_diff = json_diff(module_enums(base_mod), module_enums(cand_mod), "$enums")
    if enum_diff:
        diffs.append({"model": "*", "kind": "enum-members", "file": label, "diff": enum_diff[:40], "count": len(enum_diff)})
    for name in sorted(set(base_models) | set(cand_models)):
        if name not in base_models or name not in cand_models:
            diffs.append({"model": name, "kind": "model-set", "file": label, "left": name in base_models, "right": name in cand_models})
            continue
        left_model = base_models[name]
        right_model = cand_models[name]
        try:
            left_schema = left_model.model_json_schema()
            right_schema = right_model.model_json_schema()
        except Exception as exc:  # noqa: BLE001
            diffs.append({"model": name, "kind": "schema-error", "file": label, "error": repr(exc)})
            skipped += 1
            continue
        schema_rows = json_diff(left_schema, right_schema)
        if schema_rows:
            diffs.append({"model": name, "kind": "schema", "file": label, "count": len(schema_rows), "diff": schema_rows[:30]})
        meta_rows = json_diff(field_snapshot(left_model), field_snapshot(right_model), "$fields")
        if meta_rows:
            diffs.append({"model": name, "kind": "field-default-or-alias", "file": label, "count": len(meta_rows), "diff": meta_rows[:30]})
        config_rows = json_diff(dict(left_model.model_config), dict(right_model.model_config), "$config")
        if config_rows:
            diffs.append({"model": name, "kind": "config", "file": label, "diff": config_rows[:20]})
        probes: list[tuple[str, Any, bool]] = []
        try:
            payload = minimal(left_schema, left_schema)
            probes.append(("minimal", payload, False))
            probes.append(("construct", payload, True))
            if isinstance(payload, dict):
                probes.append(("extra", {**payload, "compatExtraProbe": 1}, False))
                renamed = dict(payload)
                changed = False
                for field_name, field in left_model.model_fields.items():
                    alias = field.alias or field_name
                    if alias != field_name and alias in renamed:
                        renamed[field_name] = renamed.pop(alias)
                        changed = True
                if changed:
                    probes.append(("python-name", renamed, False))
            properties = left_schema.get("properties") or {}
            if not properties and left_schema.get("type") != "object":
                for value in boundary_values(left_schema, left_schema)[:16]:
                    probes.append(("root", value, False))
            for key, prop in list(properties.items())[:40]:
                if not isinstance(payload, dict):
                    break
                for value in boundary_values(prop, left_schema)[:12]:
                    probes.append((key, {**payload, key: value}, False))
        except Exception as exc:  # noqa: BLE001
            diffs.append({"model": name, "kind": "probe-setup", "file": label, "error": repr(exc)})
            skipped += 1
            continue
        if name == "ApprovalBody":
            call = {"call_id": "c", "params_hash": "h"}
            for count in (0, 1, 32, 33):
                probes.append((f"calls-{count}", {"calls": [call] * count}, False))
            probes.append(("calls-null", {"calls": None}, False))
            probes.append(("calls-omit", {}, False))
        seen: set[str] = set()
        for probe_name, probe_payload, construct in probes:
            token = json.dumps({"n": probe_name, "p": probe_payload, "c": construct}, sort_keys=True, default=str)
            if token in seen:
                continue
            seen.add(token)
            try:
                left = outcome(left_model, probe_payload, construct=construct)
                right = outcome(right_model, probe_payload, construct=construct)
            except Exception as exc:  # noqa: BLE001
                diffs.append({"model": name, "kind": "probe-error", "file": label, "probe": probe_name, "error": repr(exc)})
                continue
            if left == right:
                equal += 1
            else:
                diffs.append(
                    {
                        "model": name,
                        "kind": "behavior",
                        "file": label,
                        "probe": probe_name,
                        "construct": construct,
                        "payload": probe_payload,
                        "left": left,
                        "right": right,
                    }
                )
    return {"file": label, "equal_probes": equal, "skipped_models": skipped, "diffs": diffs}


def compare_child(base_runtime: Path, cand_runtime: Path, base_gateway: Path, cand_gateway: Path, dest: Path) -> None:
    try:
        base_runtime_mod = load_module(base_runtime, "sdk_base_runtime")
        cand_runtime_mod = load_module(cand_runtime, "sdk_cand_runtime")
        base_gateway_mod = load_module(base_gateway, "sdk_base_gateway")
        cand_gateway_mod = load_module(cand_gateway, "sdk_cand_gateway")
        result = {
            "runtime": compare_pair(base_runtime_mod, cand_runtime_mod, "runtime"),
            "gateway": compare_pair(base_gateway_mod, cand_gateway_mod, "gateway"),
        }
    except Exception:  # noqa: BLE001 —— 子进程必须把加载失败写回父进程
        result = {"load_error": traceback.format_exc()}
    dest.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))


def snippet(path: Path, needle: str, limit: int = 8) -> list[str]:
    rows = []
    for number, line in enumerate(path.read_text().splitlines(), start=1):
        if needle in line:
            rows.append(f"{number}: {line.strip()}")
        if len(rows) >= limit:
            break
    return rows


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--compare":
        _, _, base_runtime, cand_runtime, base_gateway, cand_gateway, dest = sys.argv
        compare_child(Path(base_runtime), Path(cand_runtime), Path(base_gateway), Path(cand_gateway), Path(dest))
        return 0
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    import pydantic

    def tool_version(cmd: list[str]) -> str:
        proc = run(cmd, ROOT)
        return (proc.stdout or proc.stderr).decode(errors="replace").strip()

    summary: dict[str, Any] = {
        "python": sys.version,
        "pydantic": pydantic.VERSION,
        "mypy": tool_version([str(MYPY), "--version"]),
        "datamodel_codegen": tool_version([str(CODEGEN), "--version"]),
        "ruff": tool_version([str(RUFF), "--version"]),
        "hashes": {f"openapi-{name}": sha256(path) for name, path in OPENAPI.items()},
        "committed_hashes": {name: sha256(path) for name, path in COMMITTED.items()},
        "candidates": {},
    }
    summary["committed_mypy"] = mypy_text(list(COMMITTED.values()), out / "mypy-committed")
    summary["committed_mypy"]["classified"] = classify_mypy(summary["committed_mypy"].pop("text"), COMMITTED)
    for name, extra in GENERATOR_CANDIDATES:
        item: dict[str, Any] = {"extra": extra, "services": {}}
        for service in ("runtime", "gateway"):
            dest = out / name / f"types_{service}.py"
            item["services"][service] = generate(service, extra, dest)
            if dest.exists():
                item["services"][service]["sha256"] = sha256(dest)
                item["services"][service]["bytes"] = dest.stat().st_size
        paths = [out / name / "types_runtime.py", out / name / "types_gateway.py"]
        if all(path.exists() for path in paths):
            report = mypy_text(paths, out / "mypy" / name)
            report.pop("text", None)
            item["mypy"] = report
            item["snippets"] = {
                "calls": snippet(paths[0], "calls:"),
                "root": snippet(paths[0], "RootModel"),
                "enum_field": snippet(paths[0], 'Field("'),
            }
        summary["candidates"][name] = item
        print(f"generated {name} mypy={item.get('mypy', {}).get('errors')}", file=sys.stderr)
    for name, rewrite_enum in (("syntax-annotated", False), ("syntax-annotated-enum-default", True)):
        item = {"transform": "ast", "rewrite_enum_default": rewrite_enum, "services": {}}
        for service, source in COMMITTED.items():
            dest = out / name / f"types_{service}.py"
            rewrite_syntax(source, dest, rewrite_enum)
            item["services"][service] = {"sha256": sha256(dest), "bytes": dest.stat().st_size}
        paths = [out / name / "types_runtime.py", out / name / "types_gateway.py"]
        report = mypy_text(paths, out / "mypy" / name)
        report.pop("text", None)
        item["mypy"] = report
        item["snippets"] = {
            "calls": snippet(paths[0], "calls:"),
            "root": snippet(paths[0], "RootModel"),
            "action": snippet(paths[0], "action:"),
        }
        summary["candidates"][name] = item
        print(f"transformed {name} mypy={item['mypy']['errors']}", file=sys.stderr)
    for name in summary["candidates"]:
        paths = [out / name / "types_runtime.py", out / name / "types_gateway.py"]
        if not all(path.exists() for path in paths):
            continue
        dest = out / f"compare-{name}.json"
        proc = run(
            [
                str(VENV_PY),
                str(Path(__file__)),
                "--compare",
                str(COMMITTED["runtime"]),
                str(paths[0]),
                str(COMMITTED["gateway"]),
                str(paths[1]),
                str(dest),
            ],
            out,
        )
        item = summary["candidates"][name]
        item["compare_exit"] = proc.returncode
        if proc.returncode != 0:
            item["compare_stderr"] = proc.stderr.decode(errors="replace")[-4000:]
        elif dest.exists():
            compared = json.loads(dest.read_text())
            if "load_error" in compared:
                item["load_error"] = compared["load_error"]
            else:
                item["compare"] = {
                    label: {
                        "equal_probes": body["equal_probes"],
                        "skipped_models": body["skipped_models"],
                        "diff_count": len(body["diffs"]),
                        "diff_kinds": sorted({row["kind"] for row in body["diffs"]}),
                        "models": sorted({row["model"] for row in body["diffs"]}),
                    }
                    for label, body in compared.items()
                }
        print(f"compared {name} exit={proc.returncode}", file=sys.stderr)
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    print(out / "summary.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
