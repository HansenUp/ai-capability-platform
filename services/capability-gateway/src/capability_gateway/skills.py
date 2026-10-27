"""Skill 商店（S9，冻结契约 ②；D137～D141、D143）：服务端状态机、双人审批、平台签名、包代理下载、吊销。

状态机（skill_repo.TRANSITIONS）：submitted → scanned → evaluated → approved → listed → deprecated / revoked；
scan_failed、eval_failed、revoked 为终态。所有治理字段由服务端产生，请求体里的自报字段（扫描结果、签名等）一概不存在。

- ``POST /skills``（管理令牌 + 服务令牌 + 用户令牌；multipart ``package`` = 源目录 tgz，未签名）：安全解包 → 解析 frontmatter →
  源包按确定性 tgz 存入包存储 → 登记 submitted → 静态扫描 → scanned / scan_failed；201 ``{name, version, status}``。
  无法确定 name / version 的包 400（detail 以规则 id 开头）；名称属其他租户或版本已存在 409（版本不可变）。
- ``GET /skills/{n}/versions/{v}``：状态、清单、扫描报告、评测、审批人与时间；status=scanned 时附带评测回放所需的
  ``skill_md`` / ``eval_cases`` / ``eval_min_pass_rate``（Console 据此回放，未上架版本不提供包下载）。
- ``POST /skills/{n}/versions/{v}/eval-result``（服务令牌，Console）：网关按源包复核条数、用例 id 与通过率，
  阈值 max(包内声明, SKILL_EVAL_MIN_PASS_RATE) → evaluated / eval_failed。
- ``POST /skills/{n}/versions/{v}/approve``（服务令牌 + 审批人用户令牌）：策略服务判定 ``approve_skill``（仅 skill-reviewer 同租户、
  forbid:self_approve_skill；网关本地同样拒绝自批）→ 生成清单并签名 → 确定性 tgz 存储 → listed（approved 与 listed 两条事件同一次写入）。
- ``POST /skills/{n}/versions/{v}/revoke``（管理令牌）：吊销即时生效（包下载 410、出现在吊销列表、resolve 拒绝）。
- ``POST /skills/resolve``（服务令牌，Runtime 发布 Agent）：版本 listed 且同租户、声明能力 ⊆ tools ∪ discovery_scope
  → ``{lock, risk_level}``，否则 409。
- ``GET /skills/{n}/versions/{v}/package``（服务令牌，节点）：已签名 tgz（存储内容先按 package_sha256 复核）。
- ``GET /skills/revocations``（服务令牌）：吊销列表，随节点心跳下发。
规则升级后重扫：``python -m capability_gateway.skills rescan [--all]``。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Response, UploadFile
from pydantic import BaseModel, Field

from capability_gateway import config, policy, skill_sign, state
from capability_gateway.auth import Caller, require_gateway_admin, require_runtime_client, require_service_client, require_skill_eval_client
from capability_gateway.errors import GatewayError, responses
from capability_gateway.scan import RULES_VERSION, ScanReport, SkillPackage, parse_package, scan
from capability_gateway.scan.capabilities import lookup
from capability_gateway.scan.model import RISK_LEVELS
from capability_gateway.skill_repo import TRANSITIONS, SkillConflict, SkillEvent, SkillVersion, now
from capability_gateway.skill_sign import PackageError, canonical, sha256_hex
from capability_gateway.skill_store import key_for

router = APIRouter()
CallerDep = Annotated[Caller, Depends(require_runtime_client)]
ServiceDep = Annotated[str, Depends(require_service_client)]
MAX_UPLOAD = 2 * 1024 * 1024  # 源包（压缩后）上限
AUTH = (401, 403, 503)
SERVABLE = ("listed", "deprecated")


# ---- 模型 ----
class SubmitOut(BaseModel):
    name: str
    version: str
    status: str


class EvalOut(BaseModel):
    run_ref: str | None
    pass_rate: float | None


class SkillOut(BaseModel):
    name: str
    version: str
    tenant: str
    status: str
    manifest: dict[str, Any]
    manifest_sha256: str | None
    scan_report: dict[str, Any] | None
    eval: EvalOut | None
    submitted_by: str
    reviewed_by: str | None
    listed_at: datetime | None
    revoked_at: datetime | None
    # 仅 status=scanned：评测回放材料（SKILL.md 全文、evals/cases.jsonl 用例、包内声明阈值）
    skill_md: str | None = None
    eval_cases: list[dict[str, Any]] | None = None
    eval_min_pass_rate: float | None = None


class ScanSummary(BaseModel):
    passed: bool
    findings_count: int
    rules_version: str | None


class EvalSummary(BaseModel):
    pass_rate: float | None
    items: int | None  # 评测条数


class SkillListItem(BaseModel):
    name: str
    version: str
    status: str
    scan: ScanSummary | None
    eval: EvalSummary | None
    submitted_by: str
    reviewed_by: str | None
    listed_at: datetime | None
    revoked_at: datetime | None


class SkillList(BaseModel):
    items: list[SkillListItem]


class EvalItem(BaseModel):
    id: str = Field(min_length=1, max_length=128)
    passed: bool


class EvalResultIn(BaseModel):
    tenant: str | None = Field(default=None, min_length=1, max_length=64)
    run_ref: str = Field(min_length=1, max_length=256)  # Langfuse dataset run 引用
    pass_rate: float = Field(ge=0.0, le=1.0)
    items: list[EvalItem] = Field(max_length=1000)


class RevokeIn(BaseModel):
    reason: str = Field(default="", max_length=512)


class SkillRef(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    version: str = Field(min_length=1, max_length=32)


class ResolveIn(BaseModel):
    agent_id: str = Field(min_length=1, max_length=64)
    tenant: str = Field(min_length=1, max_length=64)
    skills: list[SkillRef] = Field(default_factory=list, max_length=50)
    tools: list[str] = Field(default_factory=list, max_length=500)  # Agent 版本快照的工具白名单（工具名或能力 id）
    discovery_scope: list[str] = Field(default_factory=list, max_length=500)  # 系统名 / 工具名 / 能力 id


class LockEntry(BaseModel):
    name: str
    version: str
    manifest_sha256: str


class ResolveOut(BaseModel):
    lock: list[LockEntry]
    risk_level: str


class Revoked(BaseModel):
    name: str
    version: str
    manifest_sha256: str | None
    revoked_at: datetime | None


class RevocationsOut(BaseModel):
    revoked: list[Revoked]
    generated_at: datetime


def _out(rec: SkillVersion) -> SkillOut:
    ev = EvalOut(run_ref=rec.eval_run_ref, pass_rate=rec.eval_pass_rate) if rec.eval_run_ref else None
    return SkillOut(
        name=rec.name,
        version=rec.version,
        tenant=rec.tenant,
        status=rec.status,
        manifest=rec.manifest,
        manifest_sha256=rec.manifest_sha256,
        scan_report=rec.scan_report,
        eval=ev,
        submitted_by=rec.submitted_by,
        reviewed_by=rec.reviewed_by,
        listed_at=rec.listed_at,
        revoked_at=rec.revoked_at,
    )


# ---- 状态机辅助 ----
def _draft_manifest(pkg: SkillPackage, tenant: str, submitted_by: str) -> dict[str, Any]:
    """上架前的清单草稿：治理字段（scan / eval / reviewed_by / signed_at）在签名时补齐。"""
    return {
        "schema": skill_sign.SCHEMA,
        "name": pkg.name,
        "version": pkg.version,
        "tenant": tenant,
        "files": skill_sign.file_entries(pkg.files),
        "capabilities": pkg.capabilities,
        "risk_level": pkg.risk_level,
        "data_class": pkg.data_class,
        "sandbox": pkg.sandbox,
        "scan": None,
        "eval": None,
        "submitted_by": submitted_by,
        "reviewed_by": None,
        "signed_at": None,
    }


async def _save(rec: SkillVersion, events: list[SkillEvent]) -> SkillVersion:
    try:
        return await state.skills.save(rec, events)
    except SkillConflict as e:
        raise GatewayError(409, e.code, str(e)) from None


def _event(rec: SkillVersion, event: str, actor: str, **detail: Any) -> SkillEvent:
    return SkillEvent(name=rec.name, version=rec.version, event=event, actor=actor, detail=detail)


async def _move(rec: SkillVersion, path: list[str], actor: str, detail: dict[str, Any] | None = None, **changes: Any) -> SkillVersion:
    """沿 path 依次迁移状态（每步须在 TRANSITIONS 中），一次写入、每步一条事件。"""
    cur = rec.status
    for s in path:
        if s not in TRANSITIONS[cur]:
            raise GatewayError(409, "invalid_state", f"{rec.name}@{rec.version} is {rec.status}; cannot move to {s}")
        cur = s
    new = rec.model_copy(update={"status": path[-1], **changes})
    return await _save(new, [_event(rec, s, actor, **(detail or {})) for s in path])


async def _get(name: str, version: str, tenant: str | None = None) -> SkillVersion:
    rec = await state.skills.get(name, version)
    if rec is None or (tenant is not None and rec.tenant != tenant):
        raise GatewayError(404, "unknown_skill", "unknown skill version")
    return rec


async def _source(rec: SkillVersion) -> SkillPackage:
    data = await state.skill_store.get(rec.source_key)
    if sha256_hex(data) != rec.source_sha256:
        raise GatewayError(502, "package_corrupted", "stored source package does not match its digest")
    return parse_package(skill_sign.read_tgz(data))


def _report_sha(report: dict[str, Any]) -> str:
    return sha256_hex(canonical(report))


# ---- 接口 ----
@router.post("/skills", status_code=201, dependencies=[Depends(require_gateway_admin)], responses=responses(*AUTH, 400, 409, 413, 502))
async def submit_skill(caller: CallerDep, package: Annotated[UploadFile, File()]) -> SubmitOut:
    """上传源包（未签名 tgz）并完成服务端扫描。"""
    principal = caller.principal()
    if not principal.sub:
        raise GatewayError(401, "user_token_required", "submitter identity required (X-AI-User-Token)")
    data = await package.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise GatewayError(413, "package_too_large", f"package exceeds {MAX_UPLOAD} bytes")
    try:
        pkg = parse_package(skill_sign.read_tgz(data))
    except PackageError as e:
        raise GatewayError(400, "invalid_package", str(e)) from None
    existing = await state.skills.get(pkg.name, pkg.version)
    if existing is not None:
        raise GatewayError(409, "version_exists", f"{pkg.name}@{pkg.version} already exists (versions are immutable)")
    source = skill_sign.build_tgz(pkg.files)
    digest = sha256_hex(source)
    rec = SkillVersion(
        name=pkg.name,
        version=pkg.version,
        tenant=principal.tenant,
        status="submitted",
        manifest=_draft_manifest(pkg, principal.tenant, principal.sub),
        source_key=key_for(principal.tenant, pkg.name, pkg.version, f"source-{digest[:16]}"),
        source_sha256=digest,
        submitted_by=principal.sub,
    )
    await state.skill_store.put(rec.source_key, source)
    try:
        await state.skills.create(rec, _event(rec, "submitted", principal.sub, source_sha256=digest, client=caller.client_id))
    except SkillConflict as e:
        raise GatewayError(409, e.code, str(e)) from None
    report = await scan(pkg, tenant=rec.tenant, catalog=state.catalog)
    rec = await _move(
        rec,
        ["scanned" if report.passed else "scan_failed"],
        "system:scanner",
        {"rules_version": RULES_VERSION, "blocking": report.blocking_rules()},
        scan_report=report.model_dump(),
        rules_version=RULES_VERSION,
    )
    return SubmitOut(name=rec.name, version=rec.version, status=rec.status)


@router.get("/skills", responses=responses(*AUTH))
async def list_skills(caller: CallerDep, status: str | None = None, name: str | None = None) -> SkillList:
    """Skill 版本列表（Console 审核页；服务令牌 + 用户令牌，服务态按用户租户过滤；可按 status / name 过滤）。"""
    tenant = None if caller.dev else caller.principal().tenant
    items: list[SkillListItem] = []
    for r in await state.skills.list(statuses=[status] if status else None, tenant=tenant):
        if name and r.name != name:
            continue
        rep = r.scan_report or {}
        scan_sum = (
            ScanSummary(passed=bool(rep.get("passed")), findings_count=len(rep.get("findings") or []), rules_version=r.rules_version)
            if r.scan_report is not None
            else None
        )
        ev = EvalSummary(pass_rate=r.eval_pass_rate, items=r.eval_items) if r.eval_run_ref else None
        items.append(
            SkillListItem(
                name=r.name,
                version=r.version,
                status=r.status,
                scan=scan_sum,
                eval=ev,
                submitted_by=r.submitted_by,
                reviewed_by=r.reviewed_by,
                listed_at=r.listed_at,
                revoked_at=r.revoked_at,
            )
        )
    return SkillList(items=items)


@router.get("/skills/revocations", responses=responses(*AUTH))
async def revocations(_: ServiceDep) -> RevocationsOut:
    """吊销列表（节点随心跳拉取，命中即 SKILL_REVOKED）。"""
    revoked = await state.skills.list(statuses=["revoked"])
    return RevocationsOut(
        revoked=[Revoked(name=r.name, version=r.version, manifest_sha256=r.manifest_sha256, revoked_at=r.revoked_at) for r in revoked],
        generated_at=now(),
    )


@router.post("/skills/resolve", responses=responses(*AUTH, 409))
async def resolve_skills(body: ResolveIn, _: ServiceDep) -> ResolveOut:
    """Agent 发布时冻结 skills_lock：每个 Skill 须 listed 且与 Agent 同租户，声明能力 ⊆ tools ∪ discovery_scope。"""
    granted = set(body.tools) | set(body.discovery_scope)
    lock: list[LockEntry] = []
    worst = 0
    if len({s.name for s in body.skills}) != len(body.skills):
        raise GatewayError(409, "duplicate_skill", "each skill may be pinned only once")
    for ref in body.skills:
        rec = await state.skills.get(ref.name, ref.version)
        label = f"{ref.name}@{ref.version}"
        if rec is None:
            raise GatewayError(409, "skill_not_found", f"skill {label} not found")
        if rec.tenant != body.tenant:
            raise GatewayError(409, "skill_tenant_mismatch", f"skill {label} belongs to another tenant")
        if rec.status != "listed" or rec.manifest_sha256 is None:
            raise GatewayError(409, "skill_not_listed", f"skill {label} is {rec.status}")
        caps = await lookup(state.catalog, list(rec.manifest.get("capabilities", [])), rec.tenant)
        for cid in rec.manifest.get("capabilities", []):
            cap = caps.get(cid)
            if cap is None or not ({cap.id, cap.name, cap.system} & granted):
                raise GatewayError(409, "capability_not_granted", f"skill {label} requires '{cid}' which the agent does not grant")
        worst = max(worst, RISK_LEVELS.index(rec.manifest.get("risk_level", "write_high")))
        if rec.manifest.get("sandbox") in ("workspace-write", "full-access"):
            worst = RISK_LEVELS.index("write_high")
        lock.append(LockEntry(name=rec.name, version=rec.version, manifest_sha256=rec.manifest_sha256))
    return ResolveOut(lock=lock, risk_level=RISK_LEVELS[worst])


@router.get("/skills/{name}/versions/{version}", responses=responses(*AUTH, 404, 502))
async def get_skill(name: str, version: str, caller: CallerDep) -> SkillOut:
    """版本状态（服务态带用户令牌时只看本租户；仅服务令牌时不过滤，供 Console 后台流程使用）。"""
    tenant = caller.user.tenant if caller.user is not None and not caller.dev else None
    rec = await _get(name, version, tenant)
    out = _out(rec)
    if rec.status == "scanned":
        pkg = await _source(rec)
        out.skill_md = pkg.files["SKILL.md"].decode("utf-8")
        out.eval_cases = pkg.cases
        out.eval_min_pass_rate = pkg.eval_min_pass_rate
    return out


@router.post("/skills/{name}/versions/{version}/eval-result", responses=responses(*AUTH, 404, 409, 502))
async def eval_result(name: str, version: str, body: EvalResultIn, client: Annotated[str, Depends(require_skill_eval_client)]) -> SkillOut:
    """评测结果回写与复核：条数 ≥ SKILL_EVAL_MIN_CASES 且覆盖包内全部用例、通过率按 items 重算一致、达到阈值 → evaluated。"""
    if body.tenant is None and (config.keycloak_enabled() or config.GATEWAY_ENV == "prod"):
        raise GatewayError(422, "eval_tenant_required", "evaluation tenant is required")
    rec = await _get(name, version, body.tenant)
    if config.GATEWAY_ENV == "prod" and not body.run_ref.startswith("langfuse:"):
        raise GatewayError(409, "eval_evidence_required", "production evaluation requires a Langfuse run reference")
    if rec.status != "scanned":
        raise GatewayError(409, "invalid_state", f"{name}@{version} is {rec.status}; eval results are accepted only when scanned")
    pkg = await _source(rec)
    ids = [i.id for i in body.items]
    expected = {c["id"] for c in pkg.cases}
    if len(set(ids)) != len(ids) or set(ids) != expected:
        raise GatewayError(409, "eval_inconsistent", "eval items must cover each package case exactly once")
    passed = sum(1 for i in body.items if i.passed)
    rate = passed / len(ids) if ids else 0.0
    if abs(rate - body.pass_rate) > 1e-6:
        raise GatewayError(409, "eval_inconsistent", f"pass_rate {body.pass_rate} does not match items ({rate:.4f})")
    threshold = max(pkg.eval_min_pass_rate or 0.0, config.SKILL_EVAL_MIN_PASS_RATE)
    ok = len(ids) >= config.SKILL_EVAL_MIN_CASES and rate >= threshold
    detail = {"run_ref": body.run_ref, "pass_rate": rate, "threshold": threshold, "cases": len(ids), "client": client}
    rec = await _move(
        rec,
        ["evaluated" if ok else "eval_failed"],
        f"client:{client}",
        detail,
        eval_run_ref=body.run_ref,
        eval_pass_rate=rate,
        eval_items=len(ids),
    )
    return _out(rec)


@router.post("/skills/{name}/versions/{version}/approve", responses=responses(*AUTH, 404, 409, 502))
async def approve_skill(name: str, version: str, caller: CallerDep) -> SkillOut:
    """审批（策略 approve_skill + 禁自批）→ 签名 → listed。"""
    reviewer = caller.principal()
    if not reviewer.sub:
        raise GatewayError(401, "user_token_required", "reviewer identity required (X-AI-User-Token)")
    rec = await _get(name, version, None if caller.dev else reviewer.tenant)
    if rec.status != "evaluated":
        raise GatewayError(409, "invalid_state", f"{name}@{version} is {rec.status}; only evaluated versions can be approved")
    if reviewer.sub == rec.submitted_by:
        raise GatewayError(403, "self_approval_forbidden", "the submitter cannot approve their own skill")
    req = policy.build_skill_request(
        reviewer, name=name, version=version, tenant=rec.tenant, risk_level=rec.manifest["risk_level"], submitted_by=rec.submitted_by
    )
    (d,) = await policy.check_batch([req])
    if d.decision != "allow":
        if d.reason == policy.UNAVAILABLE:
            raise GatewayError(403, "policy_unavailable", "policy service unavailable")
        code = "self_approval_forbidden" if "forbid:self_approve_skill" in d.policy_ids else "approval_denied"
        raise GatewayError(403, code, f"approval denied by policy ({d.reason})")
    pkg = await _source(rec)
    if rec.scan_report is None or rec.rules_version is None or rec.eval_run_ref is None:
        raise GatewayError(409, "invalid_state", "scan report and eval result are required before approval")
    manifest = rec.manifest | {
        "files": skill_sign.file_entries(pkg.files),
        "scan": {"report_sha256": _report_sha(rec.scan_report), "rules_version": rec.rules_version},
        "eval": {"run_ref": rec.eval_run_ref, "pass_rate": rec.eval_pass_rate},
        "reviewed_by": reviewer.sub,
        "signed_at": now().isoformat().replace("+00:00", "Z"),
    }
    if manifest["files"] != rec.manifest["files"]:
        raise GatewayError(502, "package_corrupted", "source package differs from the submitted manifest")
    body = canonical(manifest)
    sig = skill_sign.sign(body)
    msha = sha256_hex(body)
    package = skill_sign.build_tgz(pkg.files | {skill_sign.MANIFEST: body, skill_sign.SIGNATURE: canonical(sig)})
    key = key_for(rec.tenant, name, version, f"package-{msha[:16]}")
    await state.skill_store.put(key, package)
    rec = await _move(
        rec,
        ["approved", "listed"],
        reviewer.sub,
        {"manifest_sha256": msha, "kid": sig["kid"], "decision_policy_ids": list(d.policy_ids)},
        manifest=manifest,
        manifest_sha256=msha,
        signature=sig,
        kid=sig["kid"],
        package_key=key,
        package_sha256=sha256_hex(package),
        reviewed_by=reviewer.sub,
        listed_at=now(),
    )
    return _out(rec)


@router.post("/skills/{name}/versions/{version}/revoke", dependencies=[Depends(require_gateway_admin)], responses=responses(403, 404, 409))
async def revoke_skill(name: str, version: str, caller: CallerDep, body: RevokeIn | None = None) -> SkillOut:
    """吊销（即时生效）；已吊销时幂等返回。"""
    principal = caller.principal()
    rec = await _get(name, version, principal.tenant)
    if rec.status == "revoked":
        return _out(rec)
    reason = body.reason if body else ""
    rec = await _move(rec, ["revoked"], principal.sub or "admin", {"reason": reason}, revoked_at=now(), revoke_reason=reason or None)
    return _out(rec)


@router.get(
    "/skills/{name}/versions/{version}/package",
    response_class=Response,
    responses=responses(*AUTH, 404, 410, 502) | {200: {"content": {"application/gzip": {}}, "description": "已签名 Skill 包"}},
)
async def get_package(name: str, version: str, _: ServiceDep) -> Response:
    """已签名包（只经网关下发）；吊销 410，未上架 404。"""
    rec = await _get(name, version)
    if rec.status == "revoked":
        raise GatewayError(410, "skill_revoked", f"{name}@{version} has been revoked")
    if rec.status not in SERVABLE or rec.package_key is None or rec.manifest_sha256 is None:
        raise GatewayError(404, "skill_not_listed", f"{name}@{version} is {rec.status}")
    data = await state.skill_store.get(rec.package_key)
    if sha256_hex(data) != rec.package_sha256:
        raise GatewayError(502, "package_corrupted", "stored package does not match its digest")
    return Response(content=data, media_type="application/gzip", headers={"X-Skill-Manifest-Sha256": rec.manifest_sha256})


# ---- 规则升级后重扫 ----
async def rescan(*, force: bool = False) -> list[dict[str, Any]]:
    """重扫规则版本落后（force 时全部）的在途 / 已上架版本：上架前失败 → scan_failed；listed / deprecated 失败 → revoked。"""
    out: list[dict[str, Any]] = []
    for rec in await state.skills.list(statuses=["scanned", "evaluated", "approved", "listed", "deprecated"]):
        if rec.rules_version == RULES_VERSION and not force:
            continue
        pkg = await _source(rec)
        report: ScanReport = await scan(pkg, tenant=rec.tenant, catalog=state.catalog)
        detail = {"rules_version": RULES_VERSION, "previous": rec.rules_version, "blocking": report.blocking_rules()}
        changes: dict[str, Any] = {"scan_report": report.model_dump(), "rules_version": RULES_VERSION}
        if report.passed:
            await _save(rec.model_copy(update=changes), [_event(rec, "rescanned", "system:scanner", **detail)])
            result = rec.status
        elif rec.status in SERVABLE:
            reason = f"rescan failed under rules {RULES_VERSION}: {', '.join(report.blocking_rules())}"
            await _move(rec, ["revoked"], "system:scanner", detail, revoked_at=now(), revoke_reason=reason[:512], **changes)
            result = "revoked"
        else:
            await _move(rec, ["scan_failed"], "system:scanner", detail, **changes)
            result = "scan_failed"
        out.append({"name": rec.name, "version": rec.version, "status": result, "blocking": report.blocking_rules()})
    return out


async def _rescan_main(force: bool) -> list[dict[str, Any]]:
    await state.catalog.init()
    try:
        return await rescan(force=force)
    finally:
        await state.catalog.close()


def _cli(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m capability_gateway.skills")
    sub = ap.add_subparsers(dest="cmd", required=True)
    rs = sub.add_parser("rescan", help=f"按当前扫描规则（{RULES_VERSION}）重扫规则版本落后的 Skill 版本")
    rs.add_argument("--all", action="store_true", help="不论规则版本全部重扫")
    args = ap.parse_args(argv)
    result = asyncio.run(_rescan_main(args.all))
    sys.stdout.write(json.dumps({"rules_version": RULES_VERSION, "rescanned": result}, ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
