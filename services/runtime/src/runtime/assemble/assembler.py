"""Capability assembly: agent snapshot → system prompt, core tools (agent-declared ∪ skill-declared),
skill dirs, and the built-in `search_capabilities` meta-tool for second-level discovery.
（能力组装：把一个 Agent 版本快照组装为可直接注入 harness 的系统提示词、核心工具集
（agent 声明 ∪ skill 声明的并集）、技能目录列表，以及内置的 `search_capabilities`
元工具——用于两级能力发现中的第二级（按意图检索）。）
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from ai_contract import RiskLevel, SkillDir, ToolSpec
from runtime.config import settings
from runtime.state.db import AgentVersion

# 第二级能力发现的元工具：始终注入给 harness，供模型按需检索核心工具集之外的更多企业能力。
SEARCH_CAPABILITIES_TOOL = ToolSpec(
    name="search_capabilities",
    description="按意图检索当前身份可用的企业能力（第二级发现）。输入 intent 文本，返回候选工具。",
    input_schema={"type": "object", "properties": {"intent": {"type": "string"}}, "required": ["intent"]},
)

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
    "ticket.search": ToolSpec(name="ticket.search", description="查询工单", capability_id="cap.ticket.search"),
    "ticket.close_batch": ToolSpec(
        name="ticket.close_batch",
        description="批量关闭工单（高风险写）",
        capability_id="cap.ticket.close_batch",
        risk_level=RiskLevel.WRITE_HIGH,
        supports_dry_run=True,
        requires_idempotency_key=True,
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


async def discover_core_tools(subject: str, agent_id: str, declared: list[str]) -> list[ToolSpec]:
    """向能力网关按 subject/agent 查询可用的核心工具；网关不可达时回退到本地工具注册表（仅取 agent 已声明的部分）。"""
    try:
        async with httpx.AsyncClient(timeout=2.0) as c:
            r = await c.get(f"{settings.capability_gateway_url}/capabilities", params={"subject": subject, "agent": agent_id})
            r.raise_for_status()
            return [ToolSpec(**t) for t in r.json().get("tools", [])]
    except httpx.HTTPError:
        return [LOCAL_TOOLS[n] for n in declared if n in LOCAL_TOOLS]


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


async def assemble(agent: AgentVersion, subject: str) -> Assembled:
    """按 agent 版本快照与调用主体组装最终的系统提示词/工具集/技能集：
    先发现核心工具（agent 声明 ∪ 可用能力），截断到 core_tool_limit，再补上恒定存在的两个元工具。
    """
    cfg = agent.config or {}
    tools = await discover_core_tools(subject, agent.agent_id, cfg.get("tools", ["echo"]))
    skills = load_skills(cfg.get("skills", []))
    limit = int(cfg.get("core_tool_limit", 20))
    tools = tools[:limit] + [SEARCH_CAPABILITIES_TOOL, READ_ARTIFACT_TOOL]
    return Assembled(agent.system_prompt, tools, skills, limit, {"tools": [t.name for t in tools], "skills": [s.name for s in skills]})
