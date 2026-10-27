"""Skill 静态扫描（S9.3，D141 扫描规则 v1）。

规则按模块划分，每条规则有稳定 id（报告、错误体与 verify-s9 用例均按 id 断言）：
- structure.*：SKILL.md 与 frontmatter、命名、文件数与体积、扩展名白名单、拒二进制、评测集、metadata；
- remote_fetch.*：文本禁 curl / wget / pip install / git clone、非白名单 URL；脚本 AST 禁网络模块与动态执行；
- capabilities.*：声明能力须在目录且同租户、正文引用的目录工具须已声明、风险等级不得低报；
- injection.*：越权指令、bidi / 零宽字符、长 base64；
- bandit_runner（bandit.*）：脚本经 stdin 交 bandit（--ignore-nosec），HIGH 阻断；bandit 不可用时失败关闭。
severity=high 的发现阻断上架（passed=false）；其余只记录。``RULES_VERSION`` 随任何规则变化递增，
``python -m capability_gateway.skills rescan`` 据此重扫规则版本落后的版本。
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from pydantic import BaseModel

from capability_gateway.catalog import Catalog
from capability_gateway.scan import bandit_runner, capabilities, injection, remote_fetch, structure
from capability_gateway.scan.model import Finding, SkillPackage, finding
from capability_gateway.scan.structure import parse_package
from capability_gateway.skill_sign import PackageError

RULES_VERSION = "2026.10-1"
RULES: dict[str, str] = {
    **structure.RULES,
    **remote_fetch.RULES,
    **capabilities.RULES,
    **injection.RULES,
    **bandit_runner.RULES,
}

__all__ = ["RULES", "RULES_VERSION", "Finding", "ScanReport", "SkillPackage", "parse_package", "read_dir", "scan", "scan_dir"]


class ScanReport(BaseModel):
    """扫描报告：规则版本、是否通过（无 high 发现）、发现列表（按 path / line / rule 排序）。"""

    rules_version: str
    passed: bool
    findings: list[Finding]

    def blocking_rules(self) -> list[str]:
        return sorted({f.rule for f in self.findings if f.severity == "high"})


def _report(findings: list[Finding]) -> ScanReport:
    findings = sorted(findings, key=lambda f: (f.path, f.line or 0, f.rule, f.message))
    return ScanReport(rules_version=RULES_VERSION, passed=not any(f.severity == "high" for f in findings), findings=findings)


def _static(pkg: SkillPackage) -> list[Finding]:
    """不依赖能力目录的规则（含 bandit 子进程）。"""
    return [*pkg.problems, *structure.check(pkg), *remote_fetch.check(pkg), *injection.check(pkg), *bandit_runner.check(pkg)]


async def scan(pkg: SkillPackage, *, tenant: str, catalog: Catalog) -> ScanReport:
    """对已解析的包执行全部规则（静态规则在线程中运行）。"""
    static = await asyncio.to_thread(_static, pkg)
    return _report([*static, *await capabilities.check(pkg, tenant=tenant, catalog=catalog)])


def read_dir(root: Path) -> dict[str, bytes]:
    """读取 Skill 源目录（不跟随符号链接：目录内出现任何链接或特殊文件即 PackageError）。"""
    files: dict[str, bytes] = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in [*dirnames, *filenames]:
            p = Path(dirpath) / name
            rel = p.relative_to(root).as_posix()
            if p.is_symlink():
                raise PackageError("structure.archive", f"symbolic links are not allowed: {rel}")
            if name in filenames:
                if not p.is_file():
                    raise PackageError("structure.archive", f"only regular files are allowed: {rel}")
                files[rel] = p.read_bytes()
    return files


def scan_dir(path: str | Path) -> ScanReport:
    """离线扫描一个源目录（CLI / 安全用例）：执行除 capabilities.*（需要能力目录与租户）之外的全部规则。"""
    try:
        pkg = parse_package(read_dir(Path(path)))
    except PackageError as e:
        return _report([finding(e.rule, "", str(e))])
    return _report(_static(pkg))
