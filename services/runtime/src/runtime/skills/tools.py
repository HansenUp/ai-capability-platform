"""Skill 平台工具（S9.5 DEF-3 渐进披露、S9.7 沙箱；冻结契约 ⑤）：``handle(rc, call) -> ToolResult | None``。

工具代理（runtime.loop.toolbroker，WP-B）在闸口前拦截以下三个平台工具（与 search_capabilities 同法：先记 TOOL_REQUESTED，
再调 ``handle``，结果写 TOOL_RESULT；``ToolResult.event`` 为可选的观测字段，可并入 TOOL_RESULT 或单独事件，重放据事件日志
重建，不重新执行）；``handle`` 对其他工具返回 None：

- ``load_skill{name}``：返回技能 SKILL.md 全文（渐进披露：系统提示词只注入名称与描述）；
- ``read_skill_file{name, path}``：返回技能包内的文本文件（只限清单内的文件，逐字节核哈希；旧式开发技能限目录内常规文件）；
- ``run_skill_script{name, script, stdin?}``：在沙箱中执行技能 ``scripts/`` 下的脚本。沙箱级别只取已验签清单的 ``sandbox``
   声明（模型入参中的任何级别字段一律忽略，绝不升级）；写脚本须通过持久化审批、写前登记与租约校验；未签名技能拒绝。

``rc`` 只需具备 ``skills: list[SkillDir]``（本 Run 组装出的技能）与 ``workspace: str``（本 Run 工作区）两个属性。
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from ai_contract import SkillDir, ToolSpec
from runtime import sandbox
from runtime.skills import settings as skill_settings
from runtime.skills import store
from runtime.skills.verify import Manifest, SkillVerifyError, check_file, check_rel_path

LOAD_SKILL = "load_skill"
READ_SKILL_FILE = "read_skill_file"
RUN_SKILL_SCRIPT = "run_skill_script"
TOOL_NAMES = frozenset({LOAD_SKILL, READ_SKILL_FILE, RUN_SKILL_SCRIPT})

LOAD_SKILL_TOOL = ToolSpec(
    name=LOAD_SKILL,
    description="加载一个可用技能的完整说明（SKILL.md）。系统提示词只列出技能名称与描述，需要使用某个技能时先调用本工具。",
    input_schema={"type": "object", "properties": {"name": {"type": "string", "description": "技能名称"}}, "required": ["name"]},
)
READ_SKILL_FILE_TOOL = ToolSpec(
    name=READ_SKILL_FILE,
    description="读取技能包内的参考文件（如 references/format.md）。",
    input_schema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "技能名称"},
            "path": {"type": "string", "description": "技能包内的相对路径"},
        },
        "required": ["name", "path"],
    },
)
RUN_SKILL_SCRIPT_TOOL = ToolSpec(
    name=RUN_SKILL_SCRIPT,
    description="在隔离沙箱中执行技能自带的固定脚本（scripts/ 下），stdin 传入文本，返回退出码与输出。",
    input_schema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "技能名称"},
            "script": {"type": "string", "description": "脚本相对路径，如 scripts/summarize.py"},
            "stdin": {"type": "string", "description": "传给脚本的标准输入（可选）"},
        },
        "required": ["name", "script"],
    },
)


class RunLike(Protocol):
    skills: list[SkillDir]
    workspace: str


class CallLike(Protocol):
    tool_name: str
    input: dict[str, Any]


@dataclass
class ToolResult:
    status: Literal["ok", "error"]
    output: Any
    event: dict[str, Any] = field(default_factory=dict)  # 观测字段：skill / version / manifest_sha256 / sandbox_level 等


def _err(message: str, code: str | None = None, **event: Any) -> ToolResult:
    out: dict[str, Any] = {"error": message}
    if code:
        out["code"] = code
    return ToolResult("error", out, event)


def frontmatter(text: str) -> dict[str, str]:
    """SKILL.md 的 YAML 头（只取顶层 ``key: value`` 标量，足够取 name / description）。"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    out: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if ":" in line and not line.startswith((" ", "\t", "#")):
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def has_scripts(skill: SkillDir) -> bool:
    """已验签技能、清单声明可执行（sandbox ≠ none）且含 scripts/ 下的文件。"""
    try:
        m = store.manifest_of(skill)
    except SkillVerifyError:
        return False
    return m is not None and m.sandbox != "none" and any(f["path"].startswith("scripts/") for f in m.files)


def platform_tools(skills: list[SkillDir], progressive: bool) -> list[ToolSpec]:
    """组装器按需注入的平台工具：渐进披露注入 load_skill / read_skill_file；有可执行脚本的已验签技能注入 run_skill_script。"""
    out: list[ToolSpec] = []
    if progressive and skills:
        out += [LOAD_SKILL_TOOL, READ_SKILL_FILE_TOOL]
    if any(has_scripts(s) for s in skills):
        out.append(RUN_SKILL_SCRIPT_TOOL)
    return out


def _read_verified(skill: SkillDir, manifest: Manifest | None, rel: str, max_bytes: int) -> bytes:
    """读技能内文件（python 读取明文）：已验签技能逐字节核哈希；旧式开发技能限目录内常规文件、拒符号链接。"""
    check_rel_path(rel)
    root = Path(skill.path)
    p = root.joinpath(*rel.split("/"))
    if manifest is None:
        cur = root
        for part in rel.split("/"):
            cur = cur / part
            if cur.is_symlink():
                raise SkillVerifyError("SKILL_VERIFY_FAILED", f"symlink not allowed: {rel}")
        if not p.is_file():
            raise FileNotFoundError(rel)
    elif manifest.file(rel) is None:
        raise FileNotFoundError(rel)
    if p.stat().st_size > max_bytes * 4:  # 透明加密下磁盘大小可能略大于明文；先粗筛避免读入超大文件
        raise ValueError("file too large")
    data = p.read_bytes()
    if manifest is not None:
        check_file(manifest, rel, data)
    if len(data) > max_bytes:
        raise ValueError("file too large")
    return data


def _skill(rc: RunLike, name: Any) -> SkillDir | None:
    return next((s for s in getattr(rc, "skills", None) or [] if s.name == name), None)


async def handle(rc: RunLike, call: CallLike, *, operation_id: str | None = None) -> ToolResult | None:
    """平台工具拦截入口；非本模块工具返回 None。"""
    if call.tool_name not in TOOL_NAMES:
        return None
    args = call.input if isinstance(call.input, dict) else {}
    skill = _skill(rc, args.get("name"))
    if skill is None:
        return _err(f"unknown skill {args.get('name')!r}")
    try:
        if store.lock_entry_of(skill) is not None:
            await store.check_not_revoked(skill.name, skill.version)
        manifest = await asyncio.to_thread(store.manifest_of, skill)
    except SkillVerifyError as e:
        return _err(e.message, e.code, skill=skill.name, version=skill.version)
    ev: dict[str, Any] = {"skill": skill.name, "version": skill.version}
    if manifest is not None:
        ev["manifest_sha256"] = manifest.sha256
        ev["data_class"] = manifest.data_class
    max_bytes = skill_settings.load().read_max_bytes

    if call.tool_name in (LOAD_SKILL, READ_SKILL_FILE):
        rel = "SKILL.md" if call.tool_name == LOAD_SKILL else args.get("path")
        if not isinstance(rel, str):
            return _err("path is required", **ev)
        try:
            data = await asyncio.to_thread(_read_verified, skill, manifest, rel, max_bytes)
            text = data.decode("utf-8")
        except SkillVerifyError as e:
            return _err(e.message, e.code, **ev)
        except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
            return _err(f"file {rel!r} not found in skill {skill.name}", **ev)
        except UnicodeDecodeError:
            return _err(f"file {rel!r} is not UTF-8 text", **ev)
        except (ValueError, OSError) as e:
            return _err(f"file {rel!r} cannot be read: {e}", **ev)
        out: dict[str, Any] = {"name": skill.name, "version": skill.version, "content": text}
        if call.tool_name == READ_SKILL_FILE:
            out["path"] = rel
        return ToolResult("ok", out, ev | {"path": rel, "sha256": hashlib.sha256(data).hexdigest()})

    # run_skill_script
    script = args.get("script")
    if manifest is None:
        return _err("scripts of unsigned (development) skills cannot be executed", sandbox.SANDBOX_DENIED, **ev)
    level = manifest.sandbox  # 只取清单声明，绝不采信模型入参
    ev["sandbox_level"] = level
    if level == "none":
        return _err(f"skill {skill.name} declares sandbox=none; scripts cannot be executed", sandbox.SANDBOX_DENIED, **ev)
    if level != "read-only":
        from runtime.skills.execution import authorize

        try:
            await authorize(rc, call, operation_id)
        except SkillVerifyError as exc:
            return _err(exc.message, sandbox.SANDBOX_DENIED, **ev)
    if not isinstance(script, str) or not script.startswith("scripts/") or manifest.file(script) is None:
        return _err(f"script {script!r} is not a declared script of skill {skill.name}", sandbox.SANDBOX_DENIED, **ev)
    stdin = args.get("stdin")
    if stdin is not None and not isinstance(stdin, str):
        return _err("stdin must be a string", **ev)
    try:
        bundle = {f["path"]: await asyncio.to_thread(_read_verified, skill, manifest, f["path"], 1 << 30) for f in manifest.files}
    except (SkillVerifyError, OSError, ValueError) as e:
        return _err(f"skill files failed verification: {e}", "SKILL_VERIFY_FAILED", **ev)
    entry = manifest.file(script) or {}
    try:
        execution_options: dict[str, Any] = {}
        if level != "read-only":
            execution_options = {"operation_id": operation_id, "before_start": lambda: authorize(rc, call, operation_id)}
        res = await sandbox.run(
            script, level, stdin or "", rc.workspace, files=bundle, expected_sha256=entry.get("sha256"), **execution_options
        )
    except sandbox.SandboxError as e:
        return _err(e.message, e.code, **ev)
    out = {"exit_code": res.exit_code, "stdout": res.stdout, "stderr": res.stderr, "timed_out": res.timed_out}
    ok = res.exit_code == 0 and not res.timed_out
    return ToolResult("ok" if ok else "error", out, ev | {"script": script, "exit_code": res.exit_code, "timed_out": res.timed_out})
