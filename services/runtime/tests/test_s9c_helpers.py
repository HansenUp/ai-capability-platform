"""S9 WP-C 测试辅助：生成 ES256 签名密钥 / JWKS、按冻结契约 ① 构造清单与签名、确定性 tgz、假网关。

本文件不含用例（pytest 收集为空）；其他 test_s9c_* 用例从这里导入。
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import tarfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

from runtime.skills.verify import canonical, jwk_thumbprint

SKILL_MD = """---
name: demo-skill
description: 演示技能：总结工单要点（只读）。
---

# 演示技能正文
BODY-MARKER：只有 load_skill 才能看到这段正文。
"""
SCRIPT = b"import sys\ndata = sys.stdin.read()\nprint('summary:' + data.upper())\n"


def b64url(n: int) -> str:
    return base64.urlsafe_b64encode(n.to_bytes(32, "big")).rstrip(b"=").decode()


class Signer:
    def __init__(self) -> None:
        self.key = ec.generate_private_key(ec.SECP256R1())
        nums = self.key.public_key().public_numbers()
        self.jwk = {"kty": "EC", "crv": "P-256", "x": b64url(nums.x), "y": b64url(nums.y)}
        self.kid = jwk_thumbprint(self.jwk)

    def sign(self, data: bytes) -> bytes:
        sig = self.key.sign(data, ec.ECDSA(hashes.SHA256()))
        return json.dumps({"alg": "ES256", "kid": self.kid, "sig": base64.b64encode(sig).decode()}).encode()


def write_jwks(path: Path, *signers: Signer) -> Path:
    path.write_text(json.dumps({"keys": [s.jwk | {"kid": s.kid, "use": "sig"} for s in signers]}), encoding="utf-8")
    return path


def default_files(script: bytes = SCRIPT) -> dict[str, bytes]:
    return {
        "SKILL.md": SKILL_MD.encode(),
        "references/format.md": "# 格式\n问题 / 根因 / 处理\n".encode(),
        "scripts/summarize.py": script,
    }


def manifest_bytes(
    files: dict[str, bytes],
    *,
    name: str = "demo-skill",
    version: str = "1.0.0",
    tenant: str = "default",
    sandbox: str = "read-only",
    capabilities: list[str] | None = None,
    **override: Any,
) -> bytes:
    obj: dict[str, Any] = {
        "schema": "ai-platform.skill-manifest/v1",
        "name": name,
        "version": version,
        "tenant": tenant,
        "files": [{"path": p, "sha256": hashlib.sha256(d).hexdigest(), "size": len(d)} for p, d in sorted(files.items())],
        "capabilities": capabilities if capabilities is not None else ["cap.ticket.search"],
        "risk_level": "read",
        "data_class": "internal",
        "sandbox": sandbox,
        "scan": {"report_sha256": "0" * 64, "rules_version": "1"},
        "eval": {"run_ref": "langfuse:skill-demo/r1", "pass_rate": 1.0},
        "submitted_by": "alice",
        "reviewed_by": "bob",
        "signed_at": "2026-10-18T00:00:00Z",
    }
    obj.update(override)
    return canonical(obj)


def build(signer: Signer, files: dict[str, bytes] | None = None, **kw: Any) -> tuple[dict[str, bytes], str]:
    """返回 (包内全部文件含 manifest.json / manifest.sig, manifest_sha256)。"""
    files = dict(files or default_files())
    mb = manifest_bytes(files, **kw)
    return files | {"manifest.json": mb, "manifest.sig": signer.sign(mb)}, hashlib.sha256(mb).hexdigest()


def tgz(files: dict[str, bytes], *, extra: Callable[[tarfile.TarFile], None] | None = None) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for p in sorted(files):
            ti = tarfile.TarInfo(p)
            ti.size, ti.mode, ti.mtime = len(files[p]), 0o644, 0
            tar.addfile(ti, io.BytesIO(files[p]))
        if extra is not None:
            extra(tar)
    return buf.getvalue()


class FakeGateway:
    """假能力网关：package / revocations；记录请求。"""

    def __init__(self) -> None:
        self.packages: dict[tuple[str, str], bytes] = {}
        self.revoked: list[dict[str, Any]] = []
        self.revocations_status = 200
        self.calls: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(f"{request.method} {path}")
        if path == "/skills/revocations":
            if self.revocations_status != 200:
                return httpx.Response(self.revocations_status)
            return httpx.Response(200, json={"revoked": self.revoked, "generated_at": "2026-10-18T00:00:00Z"})
        parts = path.strip("/").split("/")
        if len(parts) == 5 and parts[0] == "skills" and parts[2] == "versions" and parts[4] == "package":
            data = self.packages.get((parts[1], parts[3]))
            return httpx.Response(200, content=data) if data is not None else httpx.Response(404, json={"detail": "not found"})
        return httpx.Response(503, json={"detail": "unavailable"})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def downloads(self) -> int:
        return sum(1 for c in self.calls if c.endswith("/package"))
