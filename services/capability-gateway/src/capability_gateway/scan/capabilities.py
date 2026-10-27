"""能力声明规则（capabilities.*）：声明能力必须真实存在且属于上传者租户，正文用到的目录工具必须已声明，风险等级不得低报。

声明可以写能力 id（cap.ticket.search）或本租户工具名（ticket.search）。
- capabilities.unknown：声明的能力不在目录（或未发布）；
- capabilities.cross_tenant：声明的能力属于其他租户；
- capabilities.undeclared_reference：SKILL.md / references 中引用了本租户目录里的工具（名称或能力 id），但未在 metadata 中声明；
- capabilities.risk_understated：metadata 风险等级低于所声明能力的最高风险等级。
"""

from __future__ import annotations

import re

from capability_gateway.catalog import Catalog
from capability_gateway.registry import Capability
from capability_gateway.scan.model import RISK_LEVELS, Finding, SkillPackage, finding, line_of

RULES = {
    "capabilities.unknown": "声明能力不在目录或未发布",
    "capabilities.cross_tenant": "声明能力属于其他租户",
    "capabilities.undeclared_reference": "正文引用的目录工具未声明",
    "capabilities.risk_understated": "风险等级低于所声明能力",
}


def _mentions(text: str, token: str) -> int | None:
    """token 以完整标识出现（前后不是字母数字、点、下划线、连字符）时返回首次出现的偏移。"""
    m = re.search(rf"(?<![\w.-]){re.escape(token)}(?![\w-]|\.\w)", text)
    return m.start() if m else None


async def lookup(catalog: Catalog, refs: list[str], tenant: str) -> dict[str, Capability | None]:
    """声明的能力引用 → 目录能力：先按能力 id，找不到再按本租户已发布的工具名。"""
    by_id = await catalog.get_many(refs)
    out: dict[str, Capability | None] = {}
    for ref in refs:
        out[ref] = by_id.get(ref) or await catalog.by_name(ref, tenant)
    return out


async def check(pkg: SkillPackage, *, tenant: str, catalog: Catalog) -> list[Finding]:
    out: list[Finding] = []
    caps = await lookup(catalog, pkg.capabilities, tenant)
    worst = 0
    for cid in pkg.capabilities:
        cap = caps.get(cid)
        if cap is None or cap.status != "published":
            out.append(finding("capabilities.unknown", "SKILL.md", f"declared capability '{cid}' is not a published catalog capability"))
        elif cap.tenant != tenant:
            out.append(finding("capabilities.cross_tenant", "SKILL.md", f"declared capability '{cid}' belongs to another tenant"))
        else:
            worst = max(worst, RISK_LEVELS.index(cap.risk_level) if cap.risk_level in RISK_LEVELS else len(RISK_LEVELS) - 1)
    if pkg.risk_level in RISK_LEVELS and RISK_LEVELS.index(pkg.risk_level) < worst:
        out.append(
            finding(
                "capabilities.risk_understated",
                "SKILL.md",
                f"risk-level '{pkg.risk_level}' is lower than declared capabilities ({RISK_LEVELS[worst]})",
            )
        )
    declared = set(pkg.capabilities) | {c.id for c in caps.values() if c is not None}
    texts = {"SKILL.md": pkg.body} | {p: t for p, t in pkg.texts().items() if p.startswith("references/")}
    for c in await catalog.list_published():
        if c.tenant != tenant or c.id in declared:
            continue
        for path, text in sorted(texts.items()):
            offsets = [o for o in (_mentions(text, c.name), _mentions(text, c.id)) if o is not None]
            if offsets:
                line = line_of(text, min(offsets)) + (_body_offset(pkg) if path == "SKILL.md" else 0)
                out.append(
                    finding(
                        "capabilities.undeclared_reference",
                        path,
                        f"references catalog tool '{c.name}' ({c.id}) without declaring it",
                        line=line,
                    )
                )
    return out


def _body_offset(pkg: SkillPackage) -> int:
    """正文在 SKILL.md 中的起始行偏移（frontmatter 行数）。"""
    full = pkg.files["SKILL.md"].decode("utf-8", "replace")
    return full.count("\n", 0, max(0, len(full) - len(pkg.body)))
