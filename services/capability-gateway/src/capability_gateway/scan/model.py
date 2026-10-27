"""扫描的数据结构：发现（Finding）与解析后的包（SkillPackage）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, computed_field

RISK_LEVELS = ("read", "write_low", "write_high")
DATA_CLASSES = ("public", "internal", "confidential")
SANDBOXES = ("none", "read-only", "workspace-write", "full-access")


class Finding(BaseModel):
    rule: str  # 稳定规则 id（<模块>.<规则>），如 remote_fetch.network_import
    severity: Literal["high", "medium", "low"]  # high 阻断上架
    path: str  # 包内相对路径（包级问题为空串）
    line: int | None = None
    message: str

    @computed_field  # type: ignore[prop-decorator]
    @property
    def rule_id(self) -> str:
        """与 rule 相同（evals/security 约定的字段名）。"""
        return self.rule


def finding(rule: str, path: str, message: str, *, line: int | None = None, severity: str = "high") -> Finding:
    return Finding(rule=rule, severity=severity, path=path, line=line, message=message[:500])  # type: ignore[arg-type]


def line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


@dataclass
class SkillPackage:
    """解析后的 Skill 源包（未签名）。解析时可恢复的问题记入 problems（同样参与判定）。"""

    files: dict[str, bytes]
    name: str
    version: str
    description: str
    body: str  # SKILL.md 去掉 frontmatter 后的正文
    capabilities: list[str]
    risk_level: str
    data_class: str
    sandbox: str
    eval_min_pass_rate: float | None
    cases: list[dict[str, Any]]
    problems: list[Finding] = field(default_factory=list)

    def texts(self) -> dict[str, str]:
        """可按 UTF-8 解码的文件（二进制由 STR007 处理）。"""
        out: dict[str, str] = {}
        for p, b in self.files.items():
            try:
                out[p] = b.decode("utf-8")
            except UnicodeDecodeError:
                continue
        return out

    def scripts(self) -> dict[str, str]:
        return {p: t for p, t in self.texts().items() if p.endswith(".py")}
