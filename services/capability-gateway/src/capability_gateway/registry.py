"""能力目录（Capability Registry）。

平台"AI Capability Gateway"层的能力元数据来源：每个能力（MCP 工具、Skill、知识库、数据集、模型、组合能力）
在此登记风险级别、数据分级、所属系统与负责人，供两级发现（核心预载 + 意图检索）、授权判定与审计使用。
当前为内存实现，S8 工作包迁移到 SQLAlchemy + Elasticsearch 索引。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class Capability(BaseModel):
    """一条能力登记记录；`to_tool_spec()` 输出 Runtime 需要的 ToolSpec 字段。"""

    id: str  # 全局唯一能力 id，约定 cap.<系统>.<动作>
    type: Literal["mcp_tool", "skill", "knowledge", "dataset", "model", "composite"] = "mcp_tool"  # 能力类型
    name: str  # 暴露给模型的工具名
    description: str  # 面向模型与检索的中文描述
    input_schema: dict[str, Any] = Field(default_factory=dict)  # JSON Schema 入参
    system: str = ""  # 所属业务系统；"outbound" 表示出站类能力（受数据污点规则约束）
    owner: str = ""  # 负责团队
    version: str = "1.0.0"
    status: Literal["draft", "published", "retired"] = "published"  # 只有 published 参与发现
    risk_level: Literal["read", "write_low", "write_high"] = "read"  # 风险级别，决定闸口是否要求审批
    data_class: Literal["public", "internal", "confidential"] = "internal"  # 数据分级，读取后形成污点
    tenant: str = "default"
    core_for_agents: list[str] = Field(default_factory=list)  # 将本能力作为核心工具预载的 Agent 列表
    supports_dry_run: bool = False  # 是否支持 dry-run 影响预演（write_high 必须支持）
    requires_idempotency_key: bool = False  # 执行时是否要求幂等键

    def to_tool_spec(self) -> dict[str, Any]:
        """转换为 Runtime 侧 ToolSpec 的字典形式。"""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "capability_id": self.id,
            "risk_level": self.risk_level,
            "data_class": self.data_class,
            "supports_dry_run": self.supports_dry_run,
            "requires_idempotency_key": self.requires_idempotency_key,
        }


# 开发 / 演示用种子数据：覆盖只读、高风险写、机密数据、出站四类典型能力
SEED: list[Capability] = [
    Capability(
        id="cap.ticket.search",
        name="ticket.search",
        description="按条件查询售后工单（只读）",
        system="ticket",
        owner="售后系统组",
        core_for_agents=["demo"],
    ),
    Capability(
        id="cap.ticket.close_batch",
        name="ticket.close_batch",
        description="批量关闭工单并通知客户",
        system="ticket",
        owner="售后系统组",
        risk_level="write_high",  # 高风险写：需 dry-run 预演 + 人工审批 + 幂等键
        supports_dry_run=True,
        requires_idempotency_key=True,
        core_for_agents=["demo"],
    ),
    Capability(
        id="cap.crm.opportunity.search",
        name="crm.opportunity.search",
        description="查询本人有权查看的销售商机",
        system="CRM",
        owner="销售系统组",
    ),
    Capability(
        id="cap.knowledge.search",
        name="knowledge.search",
        description="企业知识库混合检索（按身份过滤）",
        type="knowledge",
        system="knowledge",
        owner="知识平台组",
    ),
    Capability(
        id="cap.data.query",
        name="data.query",
        description="对受限视图执行只读 SQL（行过滤、列脱敏、限行）",
        type="dataset",
        system="datahub",
        owner="数据平台组",
        data_class="confidential",  # 读取后 Run 带上 confidential 污点
    ),
    Capability(id="cap.mail.send", name="mail.send", description="发送邮件（外发）", system="outbound", owner="IT", risk_level="write_low"),
]


class Registry:
    """能力目录的内存实现。"""

    def __init__(self, items: list[Capability] | None = None) -> None:
        self._items: dict[str, Capability] = {c.id: c for c in (items or SEED)}

    def list(self) -> list[Capability]:
        """返回全部已发布能力。"""
        return [c for c in self._items.values() if c.status == "published"]

    def get(self, cid: str) -> Capability | None:
        """按 id 取一条能力。"""
        return self._items.get(cid)

    def upsert(self, c: Capability) -> Capability:
        """新增或覆盖一条能力。"""
        self._items[c.id] = c
        return c

    def core_for(self, agent: str) -> list[Capability]:
        """第一级发现：某 Agent 需要预载的核心能力。"""
        return [c for c in self.list() if agent in c.core_for_agents]

    def search(self, intent: str, limit: int = 5) -> list[Capability]:
        """第二级发现：按意图文本检索候选能力。

        开发态用词元重叠 + 逐字命中的简单打分；生产用 ES 混合检索（BM25 + 向量）。
        """
        terms = {t for t in intent.lower().replace("，", " ").split() if t}

        def score(c: Capability) -> int:
            hay = f"{c.name} {c.description} {c.system}".lower()
            # 词元命中数 + 意图中每个字符在描述中的命中数（对中文无分词时的粗略召回）
            return sum(1 for t in terms if t in hay) + sum(1 for ch in intent if ch in c.description)

        ranked = sorted(self.list(), key=score, reverse=True)
        return [c for c in ranked if score(c) > 0][:limit]
