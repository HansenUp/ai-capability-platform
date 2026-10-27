"""签名脚本的调用绑定、声明式影响预览与持久化执行校验。"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ai_contract import RiskLevel, RunEventType, SandboxLevel, ToolSpec, ToolUseEvent
from runtime.config import settings
from runtime.guard.gate import _approval_valid, params_hash
from runtime.sandbox import SANDBOX_DENIED
from runtime.sandbox import settings as sandbox_settings
from runtime.skills import store, tools
from runtime.skills.verify import SKILL_VERIFY_FAILED, SkillVerifyError, verify_dir
from runtime.state.db import Run, SessionLocal
from runtime.state.eventlog import EventLog, LeaseLost, current_fence

BINDING = "__skill_execution"


async def engine_identity() -> str:
    from runtime.sandbox import SandboxError, docker

    try:
        code, output, _ = await docker._exec(sandbox_settings.load(), ["info", "--format", "{{.ID}}"])
    except SandboxError as exc:
        raise SkillVerifyError(SKILL_VERIFY_FAILED, "sandbox engine is unavailable") from exc
    identity = output.decode().strip()
    if code != 0 or not identity:
        raise SkillVerifyError(SKILL_VERIFY_FAILED, "sandbox engine identity is unavailable")
    return identity


async def prepare(rc: Any, arguments: dict[str, Any], *, replay: bool = False) -> tuple[ToolSpec, dict[str, Any]]:
    skill = next((item for item in rc.skills if item.name == arguments.get("name")), None)
    if skill is None:
        raise SkillVerifyError(SKILL_VERIFY_FAILED, "unknown skill")
    await store.check_not_revoked(skill.name, skill.version)
    lock = store.lock_entry_of(skill)
    if lock is None:
        raise SkillVerifyError(SKILL_VERIFY_FAILED, "script execution requires a signed executable skill")
    manifest = await asyncio.to_thread(verify_dir, skill.path, lock)
    if manifest.sandbox == "none":
        raise SkillVerifyError(SKILL_VERIFY_FAILED, "script execution requires a signed executable skill")
    script = arguments.get("script")
    if not isinstance(script, str) or not script.startswith("scripts/") or manifest.file(script) is None:
        raise SkillVerifyError(SANDBOX_DENIED, "script is not declared in the signed manifest")
    if not isinstance(arguments.get("stdin", ""), str):
        raise SkillVerifyError(SKILL_VERIFY_FAILED, "stdin must be a string")
    await asyncio.to_thread(tools._read_verified, skill, manifest, script, 1 << 30)
    sandbox = sandbox_settings.load()
    from runtime.sandbox.docker import execution_user

    writable = manifest.sandbox != "read-only"
    if writable and (sandbox.backend != "docker" or "@sha256:" not in sandbox.image):
        raise SkillVerifyError(SKILL_VERIFY_FAILED, "write scripts require Docker and a digest-pinned image")
    if manifest.sandbox == "full-access" and not sandbox.egress_proxy:
        raise SkillVerifyError(SKILL_VERIFY_FAILED, "full-access requires a configured sandbox egress proxy")
    config_bytes = json.dumps(asdict(sandbox), sort_keys=True, separators=(",", ":")).encode()
    binding = {
        "name": manifest.name,
        "version": manifest.version,
        "manifest_sha256": manifest.sha256,
        "script_sha256": (manifest.file(script) or {})["sha256"],
        "sandbox_level": manifest.sandbox,
        "image": sandbox.image,
        "sandbox_config_hmac": hmac.new(settings.hook_digest_key.encode(), config_bytes, hashlib.sha256).hexdigest(),
        "workspace": str(await asyncio.to_thread(Path(rc.workspace).resolve)),
        "run_id": rc.run_id,
        "data_class": manifest.data_class,
        "engine_id": await engine_identity() if writable else None,
        "sandbox_user": execution_user(manifest.sandbox),
    }
    if replay and arguments.get(BINDING) != binding:
        raise SkillVerifyError(SKILL_VERIFY_FAILED, "script execution binding changed; the approved call cannot be replayed")
    effective = arguments | {BINDING: binding}
    tool = tools.RUN_SKILL_SCRIPT_TOOL.model_copy(
        update={
            "capability_id": f"skill:{manifest.name}@{manifest.version}:{script}",
            "risk_level": RiskLevel.WRITE_HIGH if writable else RiskLevel.READ,
            "sandbox_level": SandboxLevel(manifest.sandbox),
            "data_class": manifest.data_class,
            "supports_dry_run": writable,
            "requires_idempotency_key": False,
        }
    )
    return tool, effective


async def preview(rc: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    _, effective = await prepare(rc, arguments, replay=True)
    return {
        "mode": "declaration-only",
        "script_executed": False,
        "scope": effective[BINDING],
        "script": effective["script"],
        "stdin": effective.get("stdin", ""),
        "warning": "固定脚本可能修改工作区或产生外部副作用；本预览不预测文件差异，结果未知时必须人工对账，禁止重试。",
    }


async def authorize(rc: Any, call: Any, operation_id: str | None) -> None:
    if not operation_id:
        raise SkillVerifyError(SKILL_VERIFY_FAILED, "write script requires a durable operation record")
    _, effective = await prepare(rc, call.input, replay=True)
    async with SessionLocal() as session:
        log = EventLog(session)
        operation = await log.get_operation(operation_id)
        run = await session.get(Run, rc.run_id)
        fence = current_fence(rc.run_id)
        if run is not None and fence is not None and run.lease_epoch != fence:
            raise LeaseLost("script execution lease has been superseded")
        if (
            run is None
            or run.state != "working"
            or operation is None
            or operation.run_id != rc.run_id
            or operation.call_id != call.call_id
            or operation.status != "executing"
            or (fence is not None and operation.epoch != fence)
        ):
            raise SkillVerifyError(SKILL_VERIFY_FAILED, "write script operation is not executing for this run and call")
        events = await log.list(rc.run_id)
        approvals = [
            event.payload for event in events if event.type == RunEventType.TOOL_APPROVED and event.payload.get("call_id") == call.call_id
        ]
        executing = [
            event.payload for event in events if event.type == RunEventType.TOOL_EXECUTING and event.payload.get("call_id") == call.call_id
        ]
        fingerprint = params_hash(effective)
        if (
            not approvals
            or not _approval_valid(approvals[-1], fingerprint)
            or not executing
            or executing[-1].get("operation_id") != operation_id
            or executing[-1].get("params_hash") != fingerprint
        ):
            raise SkillVerifyError(SKILL_VERIFY_FAILED, "write script approval or write-ahead event is missing or stale")


async def execute(rc: Any, arguments: dict[str, Any], call_id: str, operation_id: str | None) -> tools.ToolResult:
    call = ToolUseEvent(call_id=call_id, tool_name=tools.RUN_SKILL_SCRIPT, input=arguments)
    result = await tools.handle(rc, call, operation_id=operation_id)
    if result is None:
        raise RuntimeError("script handler did not return a result")
    if result.status != "ok":
        raise RuntimeError(f"script did not complete successfully; effects may be partial: {result.output}")
    return result


async def stop_operation(operation_id: str | None, binding: dict[str, Any]) -> None:
    if not operation_id:
        raise ValueError("script reconciliation requires an operation id")
    from runtime.sandbox import docker

    sandbox = sandbox_settings.load()
    try:
        identity = await engine_identity()
    except SkillVerifyError as exc:
        raise ValueError("cannot reach the original sandbox engine; reconciliation is refused") from exc
    if identity != binding.get("engine_id"):
        raise ValueError("reconciliation must use the original sandbox engine")
    config_bytes = json.dumps(asdict(sandbox), sort_keys=True, separators=(",", ":")).encode()
    digest = hmac.new(settings.hook_digest_key.encode(), config_bytes, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(digest, str(binding.get("sandbox_config_hmac", ""))):
        raise ValueError("restore the approved sandbox configuration before reconciling a script")
    code, output, _ = await docker._exec(sandbox, ["ps", "-aq", "--filter", f"label=ai-platform.operation={operation_id}"])
    if code != 0:
        raise ValueError("cannot inspect the original script sandbox; reconciliation is refused")
    containers = output.decode().split()
    if containers:
        code, _, _ = await docker._exec(sandbox, ["rm", "-f", "-v", *containers])
        if code != 0:
            raise ValueError("cannot terminate the original script sandbox; reconciliation is refused")
