"""注入给 harness 的工具 / 技能（Skill）描述符（Runtime Contract 六要素之一：Tool Injection 工具注入）。

对应设计文档中的两级能力发现（two-level capability discovery）：本模块的 ToolSpec 描述的是
第一级（agent 声明 ∪ skill 声明的核心工具集），第二级发现（按意图检索更多能力）由
`runtime.assemble.assembler` 中的 `search_capabilities` 元工具实现。
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class RiskLevel(StrEnum):
    """工具调用的风险等级，驱动 gate 中的审批策略。"""

    READ = "read"  # 只读，无副作用
    WRITE_LOW = "write_low"  # 低风险写操作
    WRITE_HIGH = "write_high"  # 高风险写操作，默认需要人工审批


class SandboxLevel(StrEnum):
    """Fail-closed per-call sandbox policy. If the requested level is unavailable the call is refused.
    （按调用粒度生效的沙箱策略，失败即拒绝：若请求的沙箱级别不可用，则直接拒绝该次调用，
    绝不擅自降级或跳过沙箱要求。）
    """

    READ_ONLY = "read-only"  # 只读沙箱
    WORKSPACE_WRITE = "workspace-write"  # 仅允许写入工作区
    FULL_ACCESS = "full-access"  # 完全访问权限


class ToolSpec(BaseModel):
    """一个工具的完整描述符，用于注入 harness 以及供 gate 做风险/沙箱判定。"""

    model_config = {"extra": "forbid"}

    name: str  # 工具名（唯一标识）
    description: str  # 工具描述，供模型理解何时调用
    input_schema: dict[str, Any] = Field(default_factory=dict)  # 输入参数的 JSON Schema
    capability_id: str | None = None  # 对应能力网关中的能力标识
    risk_level: RiskLevel = RiskLevel.READ  # 风险等级，默认只读
    sandbox_level: SandboxLevel = SandboxLevel.READ_ONLY  # 所需沙箱级别，默认只读
    data_class: str = "internal"  # public | internal | confidential（数据分类，用于 taint 传播判定）
    supports_dry_run: bool = False  # 是否支持 dry-run（用于高风险写操作的影响预览）
    requires_idempotency_key: bool = False  # 是否强制要求幂等键


class SkillDir(BaseModel):
    """An Agent Skills spec package: a directory containing SKILL.md (+ scripts/ references/ assets/).
    （Agent Skills 规范包：一个包含 SKILL.md 及可选 scripts/ references/ assets/ 子目录的技能目录。）
    """

    model_config = {"extra": "forbid"}

    name: str  # 技能名称
    version: str  # 技能版本
    path: Path  # 技能目录路径
    signature: str | None = None  # 可选的签名，用于校验技能包完整性
    capabilities_declared: list[str] = Field(default_factory=list)  # 该技能声明依赖/暴露的能力列表
