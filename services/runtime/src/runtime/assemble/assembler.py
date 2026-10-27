"""Capability assembly: agent snapshot → system prompt, core tools (agent-declared ∪ skill-declared),
skill dirs, and — when the snapshot enables discovery — the `search_capabilities` meta-tool.
（能力组装：把一个 Agent 版本快照组装为可直接注入 harness 的系统提示词、核心工具集
（agent 声明 ∪ skill 声明的并集）、技能目录列表；版本快照开启 `discovery.enabled` 时再注入
`search_capabilities` 元工具——两级能力发现中的第二级（按意图检索，见 runtime.loop.discovery）。）

技能加载（S9.5，D144/D145）：快照带 ``skills_lock``（发布时经网关 resolve 冻结的 ``[{name, version, manifest_sha256}]``）时，
按 lock 从 ``SKILLS_DIR`` 加载已验签的技能（缺包经网关懒拉取并原子物化；验签 / 哈希失败抛 ``SkillVerifyError``，Run 以
``SKILL_VERIFY_FAILED`` 失败；命中吊销列表为 ``SKILL_REVOKED``），不再读仓库 ``skills/`` 目录；无 ``skills_lock`` 的旧快照
沿用开发态行为（读 ``skills/<name>``）。``skills_mode=progressive`` 时系统提示词只注入技能名称与描述，正文经平台工具
``load_skill`` / ``read_skill_file`` 按需读取；已验签且声明了可执行脚本的技能注入 ``run_skill_script``（见 runtime.skills.tools）。
技能正文、名称描述与交给 harness 的原生目录统一继承签名清单的数据等级；未经独立签署的元数据不得降级。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from pydantic import ValidationError

from ai_contract import RiskLevel, SkillDir, ToolSpec
from ai_contract.data_class import Level
from runtime import gateway_client
from runtime.skills import store as skill_store
from runtime.skills import tools as skill_tools
from runtime.skills.verify import SKILL_VERIFY_FAILED, SkillVerifyError
from runtime.state.db import AgentVersion

_TOOLSPEC_FIELDS = frozenset(ToolSpec.model_fields)

# 用于读取被外置到工作区的大体积工具结果（配合 context.budget.offload_if_large 使用）。
READ_ARTIFACT_TOOL = ToolSpec(
    name="read_artifact",
    description="读取被外置到工作区的大体积工具结果。",
    input_schema={"type": "object", "properties": {"ref": {"type": "string"}}, "required": ["ref"]},
)

# Local registry for dev; production pulls from the capability gateway Discovery API.
# （开发环境本地工具注册表；生产环境应从能力网关的 Discovery API 拉取。）
LOCAL_TOOLS: dict[str, ToolSpec] = {
    "echo": ToolSpec(
        name="echo",
        description="回显输入（开发用）",
        input_schema={"type": "object", "properties": {"text": {"type": "string", "description": "要回显的文本"}}},
    ),
    # 工单三件套的入参 schema 与能力网关种子数据一致（见 capability_gateway.registry.SEED，由网关测试校验）
    "ticket.search": ToolSpec(
        name="ticket.search",
        description="按关键字、状态与创建时间范围查询售后工单（只读，只返回调用者有权查看的工单摘要，按创建时间倒序）",
        capability_id="cap.ticket.search",
        input_schema={
            "type": "object",
            "properties": {
                "q": {"type": "string", "description": "关键字，匹配标题、描述与分类（子串，不区分大小写）"},
                "status": {"type": "string", "enum": ["open", "processing", "closed"], "description": "状态：open / processing / closed"},
                "created_from": {
                    "type": "string",
                    "description": "创建时间下限（含），ISO 8601，如 2026-10-01 或 2026-10-01T09:00:00+08:00；不带时区按东八区",
                },
                "created_to": {
                    "type": "string",
                    "description": "创建时间上限，ISO 8601；只写日期（如 2026-10-15）时含当天全天，带时间时含该时刻；不带时区按东八区",
                },
                "cursor": {"type": "string", "description": "分页游标（上一页返回的 next_cursor）"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "description": "条数，默认 20，最多 50"},
            },
        },
    ),
    "ticket.get": ToolSpec(
        name="ticket.get",
        description="按工单号查询工单详情",
        capability_id="cap.ticket.get",
        input_schema={"type": "object", "properties": {"id": {"type": "string", "description": "工单号，如 T-1001"}}, "required": ["id"]},
    ),
    # 数据分级演示用（与能力网关种子数据一致）：机密数据查询、知识检索、外发
    "data.query": ToolSpec(
        name="data.query", description="受限视图只读查询（机密）", capability_id="cap.data.query", data_class="confidential"
    ),
    "knowledge.search": ToolSpec(name="knowledge.search", description="知识库检索", capability_id="cap.knowledge.search"),
    "mail.send": ToolSpec(name="mail.send", description="发送邮件（外发）", capability_id="cap.mail.send", risk_level=RiskLevel.WRITE_LOW),
    "ticket.close_batch": ToolSpec(
        name="ticket.close_batch",
        description="批量关闭工单（高风险写）",
        capability_id="cap.ticket.close_batch",
        risk_level=RiskLevel.WRITE_HIGH,
        supports_dry_run=True,
        requires_idempotency_key=True,
        input_schema={
            "type": "object",
            "properties": {
                "ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 100,
                    "description": '待关闭的工单号列表，如 ["T-1001", "T-1002"]',
                },
            },
            "required": ["ids"],
        },
    ),
}


@dataclass
class Assembled:
    """一次能力组装的结果：系统提示词、最终工具集、技能目录集合，以及组装报告（用于事件日志观测）。"""

    system_prompt: str  # 系统提示词
    tools: list[ToolSpec]  # 最终注入 harness 的工具集合（含元工具）
    skills: list[SkillDir]  # 已加载的技能目录集合
    core_tool_limit: int = 20  # 核心工具数量上限
    report: dict[str, Any] = field(default_factory=dict)  # 组装报告，写入 CONTEXT_ASSEMBLED 事件
    resources: dict[str, dict[str, Any]] = field(default_factory=dict)  # 工具名 → 目录资源属性 {system, tenant}（闸口策略请求用）
    data_class: str = "public"


async def discover_core_tools(
    subject: str, agent_id: str, declared: list[str], resources: dict[str, dict[str, Any]] | None = None, user_token: str | None = None
) -> list[ToolSpec]:
    """按 Agent 版本快照声明的工具白名单组装工具集（S4.8 固定白名单）：

    经网关客户端（契约 3）向能力网关查询白名单中已登记、已发布的能力（取网关上的权威元数据：风险等级、数据分级、入参
    schema）；目录项附带的资源属性（system、tenant）写入 resources（闸口策略请求用），不进 ToolSpec。只在进程内执行的
    开发工具（echo）取本地定义；白名单之外的工具一律不暴露，网关侧的 core_for_agents 不能额外加工具。
    网关不可达时回退到本地工具注册表（同样只取白名单内的部分）。"""
    names = [n for n in declared if n]
    remote: dict[str, ToolSpec] = {}
    meta: dict[str, dict[str, Any]] = {}
    try:
        params = {"subject": subject, "agent": agent_id, "names": ",".join(names)}
        r = await gateway_client.request("GET", "/capabilities", user_token=user_token, timeout_s=2.0, params=params)
        r.raise_for_status()
        for t in r.json().get("tools", []):
            if not isinstance(t, dict) or t.get("name") not in names:
                continue
            try:
                remote[t["name"]] = ToolSpec(**{k: v for k, v in t.items() if k in _TOOLSPEC_FIELDS})
            except ValidationError:
                continue  # 目录项不合规：不暴露（失败关闭）
            meta[t["name"]] = {k: str(t[k]) for k in ("system", "tenant") if t.get(k)}
    except httpx.HTTPError:
        remote, meta = {}, {}
    out: list[ToolSpec] = []
    for n in names:
        if n in remote:
            out.append(remote[n])
            if resources is not None and meta.get(n):
                resources[n] = meta[n]
        elif n in LOCAL_TOOLS and (not remote or n == "echo"):
            out.append(LOCAL_TOOLS[n])
    return out


def load_skills(skill_refs: list[dict[str, str]], base: Path = Path("skills")) -> list[SkillDir]:
    """按 agent 配置中声明的技能引用加载技能目录；只有存在 SKILL.md 的目录才会被采纳。"""
    out: list[SkillDir] = []
    for ref in skill_refs:
        d = base / ref["name"]
        if (d / "SKILL.md").exists():
            out.append(SkillDir(name=ref["name"], version=ref.get("version", "0"), path=d))
    return out


def skills_digest(skills: list[SkillDir]) -> str:
    """计算一组技能的总摘要：先对每个技能目录下 SKILL.md 的内容取 sha256，
    再把排序后的 (name, digest) 列表拼成规范文本求一次 sha256，返回十六进制字符串。

    发布时把它冻结进版本快照，之后只要技能内容被改动，摘要就会变化，从而暴露"同一版本行为已漂移"。
    """
    per_skill: list[tuple[str, str]] = []
    for sk in skills:
        content = (sk.path / "SKILL.md").read_bytes()
        per_skill.append((sk.name, hashlib.sha256(content).hexdigest()))
    canon = "\n".join(f"{name}:{digest}" for name, digest in sorted(per_skill))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


async def assemble(agent: AgentVersion, subject: str, user_token: str | None = None) -> Assembled:
    """按 agent 版本快照与调用主体组装最终的系统提示词/工具集/技能集：
    先发现核心工具（agent 声明 ∪ 可用能力），截断到 core_tool_limit，再补上元工具 read_artifact；
    版本快照开启 discovery 时在其前注入 search_capabilities，并在组装报告中记录冻结的检索范围与上限。

    技能提示词内联（S5，D84）：快照 `skills_inline=true` 时把各技能的 SKILL.md 内容内联进系统提示词（供不支持
    原生技能目录的 harness 使用）；默认关闭，系统提示词与组装报告与此前完全一致。
    """
    # 延迟导入：runtime.loop 包初始化会导入 runner，而 runner 依赖本模块
    from runtime.loop import discovery

    cfg = agent.config or {}
    declared_class = cfg.get("data_class", "public")
    if not isinstance(declared_class, str) or declared_class not in {level.value for level in Level}:
        raise SkillVerifyError(SKILL_VERIFY_FAILED, "agent config.data_class is invalid")
    resources: dict[str, dict[str, Any]] = {}
    lock = skills_lock_of(agent)
    # 先加载技能：验签 / 吊销失败直接抛 SkillVerifyError（Run 失败关闭），不必再查能力目录
    skills = await skill_store.load_locked(lock, agent.tenant_id) if lock is not None else load_skills(cfg.get("skills", []))
    tools = await discover_core_tools(subject, agent.agent_id, cfg.get("tools", ["echo"]), resources, user_token)
    limit = int(cfg.get("core_tool_limit", 20))
    progressive = cfg.get("skills_mode") == "progressive"
    # 第二级能力发现的元工具：仅在版本快照开启 discovery 时注入（拦截与检索见 runtime.loop.discovery）
    meta_tools = [discovery.TOOL, READ_ARTIFACT_TOOL] if discovery.enabled(agent) else [READ_ARTIFACT_TOOL]
    platform = skill_tools.platform_tools(skills, progressive) if (lock is not None or progressive) else []
    tools = tools[:limit] + meta_tools + platform
    report: dict[str, Any] = {"tools": [t.name for t in tools], "skills": [s.name for s in skills]}
    if lock is not None:
        report["skills_lock"] = [{"name": e["name"], "version": str(e["version"]), "manifest_sha256": e["manifest_sha256"]} for e in lock]
    if discovery.enabled(agent):
        report["discovery"] = {"scope": discovery.scope(agent), "max_tools": discovery.max_tools(agent)}
    system_prompt = agent.system_prompt
    if progressive and skills:
        system_prompt = progressive_skills(system_prompt, skills)
        report["skills_mode"] = "progressive"
    elif cfg.get("skills_inline") is True and skills:
        system_prompt = inline_skills(system_prompt, skills)
        report["skills_inline"] = True
    manifests = [skill_store.manifest_of(skill) for skill in skills]
    data_class = Level.highest([declared_class, *(manifest.data_class for manifest in manifests if manifest is not None)]).value
    report["skills_data_class"] = data_class
    return Assembled(
        system_prompt, tools, skills, limit, report, {n: m for n, m in resources.items() if any(t.name == n for t in tools)}, data_class
    )


def inline_skills(system_prompt: str, skills: list[SkillDir]) -> str:
    """把技能目录的 SKILL.md 依次以 <skill name="..." version="..."> 块追加到系统提示词之后（按快照声明顺序）。"""
    blocks = [
        f'<skill name="{sk.name}" version="{sk.version}">\n{(sk.path / "SKILL.md").read_text(encoding="utf-8").strip()}\n</skill>'
        for sk in skills
    ]
    head = system_prompt.rstrip()
    return (head + "\n\n" if head else "") + "\n\n".join(blocks)


def skills_lock_of(agent: AgentVersion) -> list[dict[str, Any]] | None:
    """快照冻结的 skills_lock（列 ``t_agent_version.skills_lock``，WP-B 迁移 0005）；未冻结（旧快照 / 开发态）返回 None。
    冻结了但不合规（非对象列表）抛 SkillVerifyError，失败关闭。"""
    lock = getattr(agent, "skills_lock", None)
    if lock is None:
        return None
    if not isinstance(lock, list) or not all(isinstance(e, dict) for e in lock):
        raise SkillVerifyError(SKILL_VERIFY_FAILED, "skills_lock is malformed")
    return lock


def progressive_skills(system_prompt: str, skills: list[SkillDir]) -> str:
    """渐进披露（DEF-3）：只把技能名称与描述（SKILL.md 头部 description）追加到系统提示词，正文经 load_skill 按需加载。"""
    lines = []
    for sk in skills:
        desc = skill_tools.frontmatter((sk.path / "SKILL.md").read_text(encoding="utf-8")).get("description", "")
        lines.append(f'<skill name="{sk.name}" version="{sk.version}">{desc}</skill>')
    block = "<available_skills>\n" + "\n".join(lines) + "\n</available_skills>\n需要使用某个技能时，先调用 load_skill 读取其完整说明。"
    head = system_prompt.rstrip()
    return (head + "\n\n" if head else "") + block
