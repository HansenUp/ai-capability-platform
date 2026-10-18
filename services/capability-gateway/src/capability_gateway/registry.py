"""能力目录的数据模型与开发种子。

平台"AI Capability Gateway"层的能力元数据来源：每个能力（MCP 工具、Skill、知识库、数据集、模型、组合能力）
在此登记风险级别、数据分级、所属系统、连接器与补偿工具，供两级发现（核心预载 + 意图检索）、授权判定与审计使用。
存储见 catalog.py（memory）与 db.py（MySQL，权威）；检索索引见 search.py（Elasticsearch）。
"""

from __future__ import annotations

import os
from typing import Any, Literal

from pydantic import BaseModel, Field


class Capability(BaseModel):
    """一条能力登记记录；`to_tool_spec()` 输出 Runtime 需要的 ToolSpec 字段。"""

    id: str = Field(min_length=1, max_length=128)  # 全局唯一能力 id，约定 cap.<系统>.<动作>
    type: Literal["mcp_tool", "skill", "knowledge", "dataset", "model", "composite"] = "mcp_tool"  # 能力类型
    name: str = Field(min_length=1, max_length=128)  # 暴露给模型的工具名（同一租户内唯一）
    description: str  # 面向模型与检索的中文描述
    input_schema: dict[str, Any] = Field(default_factory=dict)  # JSON Schema 入参（不含 dry_run / idempotency_key 控制字段）
    system: str = Field(default="", max_length=64)  # 所属业务系统；"outbound" 表示出站类能力（受数据污点规则约束）
    owner: str = Field(default="", max_length=128)  # 负责团队
    version: str = Field(default="1.0.0", max_length=32)
    status: Literal["draft", "published", "retired"] = "published"  # 只有 published 参与发现
    risk_level: Literal["read", "write_low", "write_high"] = "read"  # 风险级别，决定闸口是否要求审批
    data_class: Literal["public", "internal", "confidential"] = "internal"  # 数据分级，读取后形成污点
    tenant: str = Field(default="default", max_length=64)
    supports_dry_run: bool = False  # 是否支持 dry-run 影响预演（非只读必须支持）
    requires_idempotency_key: bool = False  # 执行时是否要求幂等键（非只读必须要求）
    compensate_tool: str | None = Field(default=None, max_length=128)  # 补偿工具名（非只读必填，须在同一连接器 manifest 中）
    connector: str | None = Field(default=None, max_length=128)  # 承载该工具的连接器名；为空时按 system 匹配
    row_version: int = 0  # 服务端维护的行版本（每次变更 +1，ES 以 external_gte 版本写入防乱序覆盖）；请求体中的值被忽略

    def to_tool_spec(self) -> dict[str, Any]:
        """转换为 Runtime 侧 ToolSpec 的字典形式（ToolSpec 禁止多余字段，这里只输出其已声明的字段）。"""
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

    def search_text(self) -> str:
        """检索与向量化使用的文本。"""
        return f"{self.name} {self.description}"


class Binding(BaseModel):
    """Agent 与能力的绑定（替代旧的 core_for_agents）：core 为第一级预载候选，discoverable 为第二级可检索范围。
    agent_id 为 "*" 表示对所有 Agent 生效。"""

    agent_id: str = Field(min_length=1, max_length=64)
    capability_id: str = Field(min_length=1, max_length=128)
    kind: Literal["core", "discoverable"] = "core"


# 开发 / 演示用种子数据：覆盖只读、高风险写、机密数据、出站四类典型能力
SEED: list[Capability] = [
    Capability(
        id="cap.ticket.search",
        name="ticket.search",
        description="按关键字、状态与创建时间范围查询售后工单（只读，只返回调用者有权查看的工单摘要，按创建时间倒序）",
        # 入参与 docs/decisions/2026-10-16-首个垂直切片场景确认.md §3.2 契约、mcp-ticket 的 SearchIn 一致
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
        system="ticket",
        owner="售后系统组",
        connector="mcp-ticket",
    ),
    Capability(
        id="cap.ticket.get",
        name="ticket.get",
        description="按工单号查询工单详情（含处理记录与客户反馈，只读）",
        input_schema={"type": "object", "properties": {"id": {"type": "string", "description": "工单号，如 T-1001"}}, "required": ["id"]},
        system="ticket",
        owner="售后系统组",
        connector="mcp-ticket",
    ),
    Capability(
        id="cap.ticket.close_batch",
        name="ticket.close_batch",
        description="批量关闭工单并通知客户",
        # 只暴露业务参数；dry_run 与 idempotency_key 由 Runtime 闸口生成、经 /invoke 注入，不由模型填写
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
        system="ticket",
        owner="售后系统组",
        risk_level="write_high",  # 高风险写：需 dry-run 预演 + 人工审批 + 幂等键
        supports_dry_run=True,
        requires_idempotency_key=True,
        compensate_tool="ticket.reopen_batch",
        connector="mcp-ticket",
    ),
    Capability(
        id="cap.ticket.reopen_batch",
        name="ticket.reopen_batch",
        description="批量重开已关闭的工单（ticket.close_batch 的补偿操作）",
        input_schema={
            "type": "object",
            "properties": {
                "ids": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 100, "description": "待重开的工单号列表"},
            },
            "required": ["ids"],
        },
        system="ticket",
        owner="售后系统组",
        risk_level="write_high",  # 补偿同样是高风险写：预演 + 审批 + 幂等键
        supports_dry_run=True,
        requires_idempotency_key=True,
        compensate_tool="ticket.close_batch",
        connector="mcp-ticket",
    ),
    Capability(
        id="cap.crm.opportunity.search",
        name="crm.opportunity.search",
        description="查询本人有权查看的销售商机",
        system="CRM",
        owner="销售系统组",
        data_class="confidential",  # 读取后 Run 带上 confidential 污点
        connector="mcp-crm",
    ),
    Capability(
        id="cap.knowledge.search",
        name="knowledge.search",
        description="企业知识库混合检索（按身份过滤）",
        type="knowledge",
        system="knowledge",
        owner="知识平台组",
        connector="mcp-knowledge",
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
    Capability(
        id="cap.mail.send",
        name="mail.send",
        description="发送邮件（外发）",
        system="outbound",
        owner="IT",
        risk_level="write_low",
        supports_dry_run=True,
        requires_idempotency_key=True,
        compensate_tool="mail.recall",
        connector="mcp-mail",
    ),
]

# 开发种子绑定：demo 预载 ticket.search / ticket.close_batch；"*" 让所有 Agent 的白名单（names）照旧可用、全部种子可被检索。
# 生产由管理员按 Agent 精确绑定（PUT /agents/{agent_id}/bindings/{capability_id}）。
SEED_BINDINGS: list[Binding] = [
    Binding(agent_id="demo", capability_id="cap.ticket.search", kind="core"),
    Binding(agent_id="demo", capability_id="cap.ticket.close_batch", kind="core"),
    *(Binding(agent_id="*", capability_id=c.id, kind="core") for c in SEED),
    *(Binding(agent_id="*", capability_id=c.id, kind="discoverable") for c in SEED),
]


def seed_connectors() -> list[dict[str, Any]]:
    """开发态连接器登记（地址取环境变量；mock 在宿主机运行）。生产由管理员经 /mcp/servers 登记。"""
    env = os.environ
    return [
        {"name": "mcp-ticket", "endpoint": env.get("MCP_TICKET_URL", "http://localhost:8091"), "protocol": "rest", "system": "ticket"},
        {
            "name": "mcp-knowledge",
            "endpoint": env.get("MCP_KNOWLEDGE_URL", "http://localhost:8092"),
            "protocol": "rest",
            "system": "knowledge",
        },
        {"name": "mcp-crm", "endpoint": env.get("MCP_CRM_URL", "http://localhost:8093"), "protocol": "rest", "system": "CRM"},
        {"name": "mcp-mail", "endpoint": env.get("MCP_MAIL_URL", "http://localhost:8094"), "protocol": "rest", "system": "outbound"},
    ]
