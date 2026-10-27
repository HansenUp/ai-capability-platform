"""结构规则（structure.*）与源包解析：Agent Skills 规范目录 + 平台 metadata。

frontmatter 必填 ``name``、``description``；``metadata``（字符串映射）中：``version``（x.y.z，缺省 1.0.0）、
``ai-platform.capabilities``（能力 id 或同租户工具名，逗号 / 空白分隔，也接受 YAML 列表）、
``ai-platform.risk-level``（read / write_low / write_high）、
``ai-platform.data-class``（public / internal / confidential）、``ai-platform.sandbox``（none / read-only / workspace-write / full-access，
缺省 none）、可选 ``ai-platform.eval-min-pass-rate``。包内必须有 ``evals/cases.jsonl``（≥ SKILL_EVAL_MIN_CASES 条、数据须 public），
可选 ``evals/eval.json`` 的 ``min_pass_rate``（与 metadata 声明取较大者，作为包内声明阈值）。
"""

from __future__ import annotations

import json
import re
from typing import Any

import yaml

from capability_gateway import config
from capability_gateway.scan.model import DATA_CLASSES, RISK_LEVELS, SANDBOXES, Finding, SkillPackage, finding
from capability_gateway.skill_sign import RESERVED, PackageError

RULES = {
    "structure.skill_md": "SKILL.md 缺失或 frontmatter 不是 YAML 映射",
    "structure.name": "name 不合规（小写字母数字与连字符，≤64）",
    "structure.description": "description 缺失或超过 1024 字符",
    "structure.size_total": "文件数或体积超限",
    "structure.size_file": "单个文件体积超限",
    "structure.path": "扩展名或位置不在白名单（.py 只允许在 scripts/）",
    "structure.binary": "二进制或非 UTF-8 内容",
    "structure.evals": "evals/cases.jsonl 缺失、格式错误、条数不足或含非 public 数据",
    "structure.metadata": "metadata（version / capabilities / risk-level / data-class / sandbox）缺失或不合规",
    "structure.sandbox": "带 scripts/ 但 sandbox 为 none",
    "structure.archive": "包无法安全读取（非 tgz、链接、绝对路径、..、重复路径）",
    "structure.reserved": "包含保留文件名（manifest.json / manifest.sig 由平台生成）",
}

NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
VERSION_RE = re.compile(r"^\d{1,6}\.\d{1,6}\.\d{1,6}$")
DEFAULT_VERSION = "1.0.0"  # metadata.version 缺省值（低级别提示，不阻断）
CAP_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
PATH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*)*$")
ALLOWED_EXT = frozenset({".md", ".txt", ".json", ".jsonl", ".yaml", ".yml", ".csv", ".tsv", ".py"})
TOP_DIRS = frozenset({"scripts", "references", "assets", "evals"})
TOP_FILES = frozenset({"SKILL.md", "LICENSE", "LICENSE.txt", "LICENSE.md"})
MAX_FILES = 64
MAX_FILE_BYTES = 256 * 1024
MAX_TOTAL_BYTES = 1024 * 1024
CASES = "evals/cases.jsonl"
EVAL_JSON = "evals/eval.json"
_FRONT = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?(.*)\Z", re.S)


def _split_caps(v: Any) -> list[str] | None:
    if isinstance(v, str):
        return [x for x in re.split(r"[\s,]+", v) if x]
    if isinstance(v, list) and all(isinstance(x, str) for x in v):
        return [x.strip() for x in v if x.strip()]
    return None


def _choice(meta: dict[str, Any], key: str, allowed: tuple[str, ...], default: str | None, problems: list[Finding]) -> str:
    v = meta.get(key, default)
    if not isinstance(v, str) or v not in allowed:
        problems.append(finding("structure.metadata", "SKILL.md", f"metadata.{key} must be one of {', '.join(allowed)}"))
        return allowed[-1]  # 不合规时按最严格取值（扫描本身已失败）
    return v


def _cases(files: dict[str, bytes], problems: list[Finding]) -> list[dict[str, Any]]:
    raw = files.get(CASES)
    if raw is None:
        problems.append(finding("structure.evals", CASES, "evals/cases.jsonl is required"))
        return []
    out: list[dict[str, Any]] = []
    ids: set[str] = set()
    for n, line in enumerate(raw.decode("utf-8", "replace").splitlines(), 1):
        if not line.strip():
            continue
        try:
            case = json.loads(line)
        except ValueError:
            problems.append(finding("structure.evals", CASES, "invalid JSON", line=n))
            continue
        cid = case.get("id") if isinstance(case, dict) else None
        if not isinstance(cid, str) or not cid or cid in ids or not isinstance(case.get("input"), str):
            problems.append(finding("structure.evals", CASES, "each case needs a unique string id and a string input", line=n))
            continue
        lists_ok = all(isinstance(case.get(k, []), list) for k in ("must_call", "must_not_call", "contains"))
        if not lists_ok:
            problems.append(finding("structure.evals", CASES, "must_call / must_not_call / contains must be lists", line=n))
            continue
        if case.get("data_class", "public") != "public":
            problems.append(finding("structure.evals", CASES, "evaluation data must be public", line=n))
            continue
        ids.add(cid)
        out.append(case)
    if len(out) < config.SKILL_EVAL_MIN_CASES:
        problems.append(finding("structure.evals", CASES, f"at least {config.SKILL_EVAL_MIN_CASES} valid cases required, got {len(out)}"))
    return out


def _rate(value: Any) -> float | None:
    try:
        rate = float(value)
    except (TypeError, ValueError):
        return None
    return rate if 0.0 <= rate <= 1.0 else None


def _declared_rate(meta: dict[str, Any], files: dict[str, bytes], problems: list[Finding]) -> float | None:
    """包内声明的评测通过率下限：metadata.ai-platform.eval-min-pass-rate 与 evals/eval.json 的 min_pass_rate 取较大者。"""
    rates: list[float] = []
    if "ai-platform.eval-min-pass-rate" in meta:
        rate = _rate(meta["ai-platform.eval-min-pass-rate"])
        if rate is None:
            problems.append(finding("structure.metadata", "SKILL.md", "metadata.ai-platform.eval-min-pass-rate must be within [0, 1]"))
        rates.append(1.0 if rate is None else rate)
    if EVAL_JSON in files:
        try:
            doc = json.loads(files[EVAL_JSON])
        except ValueError:
            doc = None
        rate = _rate(doc.get("min_pass_rate", 0.0)) if isinstance(doc, dict) else None
        if rate is None:
            problems.append(finding("structure.evals", EVAL_JSON, "evals/eval.json must be an object with min_pass_rate within [0, 1]"))
        rates.append(1.0 if rate is None else rate)
    return max(rates) if rates else None


def parse_package(files: dict[str, bytes]) -> SkillPackage:
    """解析源包。无法确定 name / version 时抛 PackageError（不建记录、直接 400）；其余问题记入 problems。"""
    raw = files.get("SKILL.md")
    if raw is None:
        raise PackageError("structure.skill_md", "SKILL.md is required at package root")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise PackageError("structure.skill_md", "SKILL.md must be UTF-8") from None
    m = _FRONT.match(text.lstrip("﻿"))
    if m is None:
        raise PackageError("structure.skill_md", "SKILL.md must start with a YAML frontmatter block")
    try:
        front = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        raise PackageError("structure.skill_md", "frontmatter is not valid YAML") from None
    if not isinstance(front, dict):
        raise PackageError("structure.skill_md", "frontmatter must be a mapping")
    name = front.get("name")
    if not isinstance(name, str) or len(name) > 64 or not NAME_RE.match(name):
        raise PackageError("structure.name", "name must be lowercase letters, digits and hyphens (≤64)")
    raw_meta = front.get("metadata")
    if isinstance(raw_meta, dict):
        meta = raw_meta
    else:
        meta = {}
    problems: list[Finding] = []
    version = meta.get("version", DEFAULT_VERSION)
    if not isinstance(version, str) or not VERSION_RE.match(version):
        raise PackageError("structure.metadata", "metadata.version must be x.y.z (quoted string)")
    if "version" not in meta:
        problems.append(
            finding("structure.metadata", "SKILL.md", f"metadata.version missing; defaulted to {DEFAULT_VERSION}", severity="low")
        )
    desc = front.get("description")
    if not isinstance(desc, str) or not desc.strip() or len(desc) > 1024:
        problems.append(finding("structure.description", "SKILL.md", "description is required (≤1024 characters)"))
        desc = desc if isinstance(desc, str) else ""
    caps = _split_caps(meta.get("ai-platform.capabilities", ""))
    if caps is None or not all(CAP_RE.match(c) for c in caps) or len(set(caps)) != len(caps):
        problems.append(finding("structure.metadata", "SKILL.md", "metadata.ai-platform.capabilities must be distinct capability ids"))
        caps = [c for c in (caps or []) if CAP_RE.match(c)]
    risk = _choice(meta, "ai-platform.risk-level", RISK_LEVELS, None, problems)
    data_class = _choice(meta, "ai-platform.data-class", DATA_CLASSES, None, problems)
    sandbox = _choice(meta, "ai-platform.sandbox", SANDBOXES, "none", problems)
    rate = _declared_rate(meta, files, problems)
    return SkillPackage(
        files=files,
        name=name,
        version=version,
        description=desc.strip(),
        body=m.group(2),
        capabilities=caps,
        risk_level=risk,
        data_class=data_class,
        sandbox=sandbox,
        eval_min_pass_rate=rate,
        cases=_cases(files, problems),
        problems=problems,
    )


def check(pkg: SkillPackage) -> list[Finding]:
    out: list[Finding] = []
    total = sum(len(b) for b in pkg.files.values())
    if len(pkg.files) > MAX_FILES or total > MAX_TOTAL_BYTES:
        out.append(
            finding(
                "structure.size_total", "", f"package has {len(pkg.files)} files / {total} bytes (limits {MAX_FILES} / {MAX_TOTAL_BYTES})"
            )
        )
    for path, data in sorted(pkg.files.items()):
        top = path.split("/", 1)[0]
        ext = "." + path.rsplit(".", 1)[1] if "." in path.rsplit("/", 1)[-1] else ""
        if path in RESERVED:
            out.append(finding("structure.reserved", path, "reserved file name; the platform generates it when signing"))
            continue
        if not PATH_RE.match(path):
            out.append(finding("structure.path", path, "path must be ASCII letters, digits, '.', '_', '-' (no hidden files)"))
        elif path not in TOP_FILES and ("/" not in path or top not in TOP_DIRS):
            out.append(finding("structure.path", path, f"files must be SKILL.md, LICENSE or under {', '.join(sorted(TOP_DIRS))}/"))
        elif path not in TOP_FILES and ext not in ALLOWED_EXT:
            out.append(finding("structure.path", path, f"extension {ext or '(none)'} not allowed"))
        elif ext == ".py" and top != "scripts":
            out.append(finding("structure.path", path, ".py files are only allowed under scripts/"))
        if len(data) > MAX_FILE_BYTES:
            out.append(finding("structure.size_file", path, f"file is {len(data)} bytes (limit {MAX_FILE_BYTES})"))
        if b"\x00" in data:
            out.append(finding("structure.binary", path, "binary content (NUL byte)"))
        else:
            try:
                data.decode("utf-8")
            except UnicodeDecodeError:
                out.append(finding("structure.binary", path, "content is not UTF-8"))
    if any(p.startswith("scripts/") for p in pkg.files) and pkg.sandbox == "none":
        out.append(finding("structure.sandbox", "SKILL.md", "packages with scripts/ must declare metadata.ai-platform.sandbox"))
    return out
