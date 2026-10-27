"""Skill 测试辅助：内存中构造源包（文件字典）与打包成 tgz。"""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric import ec

ROOT = Path(__file__).resolve().parents[3]
TICKET_SUMMARY = ROOT / "skills" / "ticket-summary"
SKILL_KEY = ec.generate_private_key(ec.SECP256R1())


def cases(n: int = 10, **extra: object) -> bytes:
    lines = [
        {"id": f"c{i:02d}", "input": f"总结工单 T-{1000 + i}", "must_call": ["ticket.get"], "must_not_call": [], "contains": ["问题"]}
        | extra
        for i in range(1, n + 1)
    ]
    return "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in lines).encode()


def skill_md(
    name: str = "demo-skill",
    *,
    version: str | None = "1.0.0",
    caps: str = "cap.ticket.search cap.ticket.get",
    risk: str = "read",
    data_class: str = "internal",
    sandbox: str = "none",
    body: str = "用 `ticket.search` 检索工单，再用 `ticket.get` 取详情并总结。",
    extra_meta: str = "",
) -> bytes:
    ver = f'  version: "{version}"\n' if version else ""
    return (
        f"---\nname: {name}\ndescription: 演示 Skill：只读检索并总结工单。\nmetadata:\n{ver}"
        f'  ai-platform.capabilities: "{caps}"\n  ai-platform.risk-level: {risk}\n  ai-platform.data-class: {data_class}\n'
        f"  ai-platform.sandbox: {sandbox}\n{extra_meta}---\n\n# 演示\n\n{body}\n"
    ).encode()


def files(name: str = "demo-skill", **md: object) -> dict[str, bytes]:
    return {"SKILL.md": skill_md(name, **md), "evals/cases.jsonl": cases(), "references/format.md": "# 模板\n- 问题：\n".encode()}


def read_dir(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def tgz(fs: dict[str, bytes], *, prefix: str = "") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path, data in fs.items():
            info = tarfile.TarInfo(prefix + path)
            info.size, info.mtime = len(data), 1700000000
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()
