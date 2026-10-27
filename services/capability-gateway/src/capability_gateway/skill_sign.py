"""Skill 包签名（S9.1 / S9.2，冻结契约 ①；D137 / D138）。

- 规范化：``canonical(obj) = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()``；
  ``manifest_sha256`` = sha256(规范化字节) 的小写十六进制。
- 签名：``manifest.sig`` = ``{"alg":"ES256","kid","sig"}``，sig 为对规范化字节做 ECDSA P-256 / SHA-256 的 DER 签名（标准 base64），
  与 ``cosign sign-blob --key`` 产物同格式（``cosign verify-blob --key <公钥 PEM> --signature <sig> manifest.json`` 可验）；
  kid = RFC 7638 JWK 指纹。
- 包：确定性 tgz（按路径排序、mtime=0、uid/gid=0、无属主名、scripts/ 下 0755 其余 0644、gzip 头 mtime=0 且不带文件名），
  内含技能文件 + manifest.json + manifest.sig。
- 私钥：``SKILL_SIGNING_KEY_FILE``（PEM，EC P-256）。开发态未配置时使用 ``~/.ai-platform/keys/skill-signing.pem``，不存在则生成
  （目录 0700、文件 0600）并告警；GATEWAY_ENV=prod 未配置拒绝启动。
- 公钥导出（供 Runtime 的 ``SKILL_TRUST_KEYS_FILE``，JWKS JSON）：``python -m capability_gateway.skill_sign jwks [--out FILE]``；
  ``pem`` 子命令导出 cosign 可用的公钥 PEM。
"""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import io
import json
import logging
import os
import sys
import tarfile
import threading
from pathlib import Path, PurePosixPath
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from capability_gateway import config
from capability_gateway.act import _public_jwk, _thumbprint

log = logging.getLogger("capability_gateway.skill_sign")

SCHEMA = "ai-platform.skill-manifest/v1"
MANIFEST = "manifest.json"
SIGNATURE = "manifest.sig"
RESERVED = frozenset({MANIFEST, SIGNATURE})

_lock = threading.Lock()
_key: ec.EllipticCurvePrivateKey | None = None


class PackageError(Exception):
    """包无法安全读取（非 gzip / tar、符号链接、绝对路径、``..``、超限等）；rule 为稳定规则 id。"""

    def __init__(self, rule: str, message: str) -> None:
        super().__init__(f"{rule}: {message}")
        self.rule = rule


# ---- 规范化与签名 ----
def canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def manifest_sha256(manifest: dict[str, Any]) -> str:
    return sha256_hex(canonical(manifest))


def file_entries(files: dict[str, bytes]) -> list[dict[str, Any]]:
    """清单 files 字段：按 path 排序的 {path, sha256, size}，不含 manifest 自身与签名。"""
    return [{"path": p, "sha256": sha256_hex(files[p]), "size": len(files[p])} for p in sorted(files) if p not in RESERVED]


def _write_new_key(path: Path) -> ec.EllipticCurvePrivateKey:
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(pem)
    return key


def load_key() -> ec.EllipticCurvePrivateKey:
    """加载签名私钥（进程内缓存）；开发态缺省文件不存在时生成。"""
    global _key
    with _lock:
        if _key is not None:
            return _key
        path = config.SKILL_SIGNING_KEY_FILE
        if not path:
            if config.GATEWAY_ENV == "prod":
                raise RuntimeError("GATEWAY_ENV=prod requires SKILL_SIGNING_KEY_FILE")
            path = config.SKILL_DEV_KEY_FILE
            if not Path(path).exists():
                log.warning("SKILL_SIGNING_KEY_FILE 未配置：在 %s 生成开发用 Skill 签名密钥（仅限开发，不入库）", path)
                _key = _write_new_key(Path(path))
                return _key
            log.warning("SKILL_SIGNING_KEY_FILE 未配置：使用开发用 Skill 签名密钥 %s", path)
        loaded = serialization.load_pem_private_key(Path(path).read_bytes(), password=None)
        if not isinstance(loaded, ec.EllipticCurvePrivateKey) or loaded.curve.name != "secp256r1":
            raise RuntimeError("SKILL_SIGNING_KEY_FILE must be an EC P-256 private key (PEM)")
        _key = loaded
        return _key


def reset_for_tests(key: ec.EllipticCurvePrivateKey | None = None) -> None:
    global _key
    with _lock:
        _key = key


def kid_of(key: ec.EllipticCurvePrivateKey) -> str:
    """RFC 7638 JWK 指纹。"""
    return _thumbprint(_public_jwk(key))


def jwks(key: ec.EllipticCurvePrivateKey | None = None) -> dict[str, Any]:
    """信任公钥（JWKS），即 Runtime 的 SKILL_TRUST_KEYS_FILE 内容。"""
    key = key or load_key()
    return {"keys": [_public_jwk(key) | {"use": "sig", "alg": "ES256", "kid": kid_of(key)}]}


def public_pem(key: ec.EllipticCurvePrivateKey | None = None) -> bytes:
    key = key or load_key()
    return key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)


def sign(manifest_bytes: bytes, key: ec.EllipticCurvePrivateKey | None = None) -> dict[str, str]:
    """对清单规范化字节签名，返回 manifest.sig 内容。"""
    key = key or load_key()
    der = key.sign(manifest_bytes, ec.ECDSA(hashes.SHA256()))
    return {"alg": "ES256", "kid": kid_of(key), "sig": base64.b64encode(der).decode()}


def verify(manifest_bytes: bytes, signature: dict[str, Any], trust: dict[str, Any]) -> bool:
    """按 kid 在信任 JWKS 中找公钥验签（参考实现，Runtime 侧 verify_dir 同算法）。"""
    if signature.get("alg") != "ES256":
        return False
    for jwk in trust.get("keys", []):
        if jwk.get("kid") != signature.get("kid") or jwk.get("kty") != "EC" or jwk.get("crv") != "P-256":
            continue
        x = int.from_bytes(base64.urlsafe_b64decode(jwk["x"] + "=" * (-len(jwk["x"]) % 4)), "big")
        y = int.from_bytes(base64.urlsafe_b64decode(jwk["y"] + "=" * (-len(jwk["y"]) % 4)), "big")
        pub = ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1()).public_key()
        try:
            pub.verify(base64.b64decode(signature.get("sig", ""), validate=True), manifest_bytes, ec.ECDSA(hashes.SHA256()))
        except (InvalidSignature, ValueError):
            return False
        return True
    return False


# ---- 确定性 tgz ----
def file_mode(path: str) -> int:
    return 0o755 if path.startswith("scripts/") else 0o644


def build_tgz(files: dict[str, bytes]) -> bytes:
    """确定性 tgz：同一组文件逐字节相同。"""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for path in sorted(files):
            data = files[path]
            info = tarfile.TarInfo(path)
            info.size, info.mtime, info.mode = len(data), 0, file_mode(path)
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tar.addfile(info, io.BytesIO(data))
    out = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=out, mtime=0, compresslevel=9) as gz:
        gz.write(raw.getvalue())
    return out.getvalue()


def _clean_path(name: str) -> str:
    p = name.removeprefix("./")
    parts = PurePosixPath(p).parts
    if not p or "\\" in p or p.startswith("/") or ".." in parts or any(x in ("", ".") for x in p.split("/")):
        raise PackageError("structure.archive", f"unsafe path in package: {name!r}")
    return p


def read_tgz(data: bytes, *, max_files: int = 64, max_total: int = 1024 * 1024) -> dict[str, bytes]:
    """安全读取 tgz：只接受普通文件（目录忽略），拒绝链接 / 设备 / 绝对路径 / ``..`` / 重复路径，并限制文件数与解压总量。
    包内所有文件共处一个顶层目录且该目录下有 SKILL.md 时去掉这层目录。"""
    files: dict[str, bytes] = {}
    total = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            for m in tar:
                if m.isdir():
                    continue
                if not m.isreg():
                    raise PackageError("structure.archive", f"only regular files are allowed: {m.name!r}")
                path = _clean_path(m.name)
                if path in files:
                    raise PackageError("structure.archive", f"duplicate path: {path!r}")
                total += m.size
                if len(files) >= max_files or total > max_total:
                    raise PackageError("structure.size_total", f"package exceeds {max_files} files or {max_total} bytes")
                f = tar.extractfile(m)
                files[path] = f.read() if f is not None else b""
    except (tarfile.TarError, OSError, EOFError, gzip.BadGzipFile) as e:
        raise PackageError("structure.archive", f"not a valid tgz: {type(e).__name__}") from None
    tops = {p.split("/", 1)[0] for p in files}
    if "SKILL.md" not in files and len(tops) == 1 and all("/" in p for p in files):
        (top,) = tops
        if f"{top}/SKILL.md" in files:
            files = {p.split("/", 1)[1]: b for p, b in files.items()}
    return files


def _cli(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m capability_gateway.skill_sign", description="Skill 签名公钥导出")
    ap.add_argument("what", choices=["jwks", "pem"], help="jwks：SKILL_TRUST_KEYS_FILE 内容；pem：cosign 公钥")
    ap.add_argument("--out", help="输出文件（缺省 stdout）")
    args = ap.parse_args(argv)
    data = json.dumps(jwks(), indent=2).encode() + b"\n" if args.what == "jwks" else public_pem()
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_bytes(data)
    else:
        sys.stdout.buffer.write(data)
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
