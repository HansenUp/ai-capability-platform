"""S9 安全用例（方案 §1 9.9）的公共辅助：坏包夹具目录、按冻结契约 ① 生成签名 Skill 目录的签名器（夹具见 conftest.py）。

- 坏包：``fixtures/skills/<name>/``（五个坏包 + ``ok-baseline`` 对照组），期望命中的规则见 ``fixtures/expected.json``。
- 签名器：每个用例生成一把 ES256（P-256）测试密钥，信任公钥写成 JWKS 并经 ``SKILL_TRUST_KEYS_FILE`` 注入（Runtime 的
  ``runtime.skills.settings`` 每次现读环境变量）。清单 / 签名格式严格按契约 ①：规范化 JSON、``files`` 按 path 排序且不含
  manifest 自身与签名、``manifest.sig = {"alg":"ES256","kid","sig"}``（DER、标准 base64）、kid 为 RFC 7638 JWK 指纹。

本文件不导入任何服务包（各服务的配置在导入期读取环境变量）；被测模块一律在用例内 ``pytest.importorskip``。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

HERE = Path(__file__).resolve().parent
FIXTURE_SKILLS = HERE / "fixtures" / "skills"
EXPECTED = json.loads((HERE / "fixtures" / "expected.json").read_text(encoding="utf-8"))
BAD_SKILLS = {k: v for k, v in EXPECTED.items() if not k.startswith("_")}
RESERVED = ("manifest.json", "manifest.sig")


def canonical(obj: Any) -> bytes:
    """契约 ① 的规范化 JSON 字节。"""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _b64url(n: int) -> str:
    return base64.urlsafe_b64encode(n.to_bytes(32, "big")).rstrip(b"=").decode()


def public_jwk(key: ec.EllipticCurvePrivateKey) -> dict[str, str]:
    """P-256 公钥 → JWK（kid = RFC 7638 指纹）。"""
    nums = key.public_key().public_numbers()
    jwk = {"crv": "P-256", "kty": "EC", "x": _b64url(nums.x), "y": _b64url(nums.y)}
    thumb = hashlib.sha256(json.dumps(jwk, sort_keys=True, separators=(",", ":")).encode()).digest()
    return jwk | {"kid": base64.urlsafe_b64encode(thumb).rstrip(b"=").decode(), "alg": "ES256", "use": "sig"}


def file_entries(root: Path) -> list[dict[str, Any]]:
    """目录下全部常规文件（不含 manifest 自身与签名）的 {path, sha256, size}，按 path 排序；以 python 读取（兼容透明加密）。"""
    out = []
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if p.is_file() and not p.is_symlink() and rel not in RESERVED:
            data = p.read_bytes()
            out.append({"path": rel, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)})
    return sorted(out, key=lambda f: f["path"])


@dataclass
class Signer:
    """测试签名器：持有一把签名密钥，信任 JWKS 写在 ``jwks_path``。"""

    key: ec.EllipticCurvePrivateKey
    jwks_path: Path

    @property
    def kid(self) -> str:
        return public_jwk(self.key)["kid"]

    def trust(self, *keys: ec.EllipticCurvePrivateKey) -> None:
        """把信任公钥集合改为给定密钥（模拟轮换 / 换签名密钥）。"""
        self.jwks_path.write_text(json.dumps({"keys": [public_jwk(k) for k in keys]}), encoding="utf-8")

    def manifest(self, root: Path, name: str, version: str = "1.0.0", **overrides: Any) -> dict[str, Any]:
        m: dict[str, Any] = {
            "schema": "ai-platform.skill-manifest/v1",
            "name": name,
            "version": version,
            "tenant": "default",
            "files": file_entries(root),
            "capabilities": ["ticket.search"],
            "risk_level": "read",
            "data_class": "public",
            "sandbox": "none",
            "scan": {"report_sha256": "0" * 64, "rules_version": "test"},
            "eval": {"run_ref": "test-run", "pass_rate": 1.0},
            "submitted_by": "frank",
            "reviewed_by": "carol",
            "signed_at": "2026-10-18T00:00:00Z",
        }
        return m | overrides

    def write_signed(self, root: Path, manifest: dict[str, Any], key: ec.EllipticCurvePrivateKey | None = None) -> dict[str, str]:
        """写 manifest.json + manifest.sig，返回 skills_lock 条目 {name, version, manifest_sha256}。"""
        k = key or self.key
        body = canonical(manifest)
        sig = base64.b64encode(k.sign(body, ec.ECDSA(hashes.SHA256()))).decode()
        (root / "manifest.json").write_bytes(body)
        (root / "manifest.sig").write_text(json.dumps({"alg": "ES256", "kid": public_jwk(k)["kid"], "sig": sig}), encoding="utf-8")
        return {"name": manifest["name"], "version": manifest["version"], "manifest_sha256": hashlib.sha256(body).hexdigest()}

    def signed_copy(self, src: Path, dest: Path, name: str | None = None, **overrides: Any) -> dict[str, str]:
        """复制技能源目录到 dest 并签名，返回 lock 条目。"""
        copy_tree(src, dest)
        return self.write_signed(dest, self.manifest(dest, name or src.name, **overrides))


def copy_tree(src: Path, dest: Path) -> None:
    """复制夹具目录：文件内容经 python 读写（本机透明加密下 shutil 的原生复制得到的是磁盘密文，扫描会把 .py 判为二进制），
    符号链接按原样保留。"""
    dest.mkdir(parents=True, exist_ok=True)
    for p in sorted(src.rglob("*")):
        q = dest / p.relative_to(src)
        if p.is_symlink():
            q.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(os.readlink(p), q)
        elif p.is_dir():
            q.mkdir(parents=True, exist_ok=True)
        elif p.is_file() and "__pycache__" not in p.parts:
            q.parent.mkdir(parents=True, exist_ok=True)
            q.write_bytes(p.read_bytes())


def new_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())
