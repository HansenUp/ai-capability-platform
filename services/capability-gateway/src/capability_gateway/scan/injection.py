"""提示注入规则（injection.*），对所有文本文件生效。

- injection.override：越权 / 覆盖指令（忽略之前的指令、绕过审批、隐瞒用户、冒充系统提示等，中英文）；
- injection.invisible_char：双向控制字符（Trojan Source）与零宽字符；
- injection.base64：长 base64 串（≥ 120 字符，可能夹带隐藏指令或载荷）。
"""

from __future__ import annotations

import re

from capability_gateway.scan.model import Finding, SkillPackage, finding, line_of

RULES = {
    "injection.override": "越权或覆盖指令",
    "injection.invisible_char": "双向控制字符或零宽字符",
    "injection.base64": "长 base64 串",
}

OVERRIDE = re.compile(
    r"(?i)("
    r"ignore\s+(all\s+|any\s+|the\s+)?(previous|prior|above|earlier|preceding)\s+(instructions|prompts|rules|messages)"
    r"|disregard\s+(all\s+|any\s+|the\s+)?(previous|prior|above|system|safety)\b"
    r"|forget\s+(all\s+|your\s+)?(previous\s+)?instructions"
    r"|override\s+(the\s+)?(system|safety|policy|policies|guardrails?)"
    r"|bypass\s+(the\s+)?(approval|policy|policies|safety|security|guardrails?)"
    r"|(do\s+not|don't|never)\s+(tell|inform|show|reveal\s+to)\s+the\s+user"
    r"|you\s+are\s+now\s+(in\s+)?(developer|dan|jailbreak|unrestricted)"
    r"|reveal\s+(your\s+|the\s+)?(system\s+prompt|hidden\s+instructions)"
    r"|<\s*/?\s*(system|im_start|im_end)\s*>"
    r"|(忽略|无视|忘记|忘掉)(之前|以上|上面|前面|先前|此前|所有)(的)?(所有)?(指令|指示|提示|规则|要求)"
    r"|绕过(审批|权限|策略|安全|闸口|限制)"
    r"|(不要|不得|别)(告诉|告知|让)用户"
    r"|(泄露|输出|打印)(系统提示|系统提示词|隐藏指令)"
    r")"
)
INVISIBLE = re.compile(r"[\u202a-\u202e\u2066-\u2069\u200b-\u200f\u2060\ufeff]")
BASE64 = re.compile(r"[A-Za-z0-9+/_-]{120,}={0,2}")


def check(pkg: SkillPackage) -> list[Finding]:
    out: list[Finding] = []
    for path, text in sorted(pkg.texts().items()):
        for m in OVERRIDE.finditer(text):
            out.append(finding("injection.override", path, f"instruction override: '{m.group(0)}'", line=line_of(text, m.start())))
        for m in INVISIBLE.finditer(text):
            out.append(
                finding(
                    "injection.invisible_char",
                    path,
                    f"invisible / bidi control character U+{ord(m.group(0)):04X}",
                    line=line_of(text, m.start()),
                )
            )
        for m in BASE64.finditer(text):
            out.append(finding("injection.base64", path, f"long base64-like run ({len(m.group(0))} chars)", line=line_of(text, m.start())))
    return out
