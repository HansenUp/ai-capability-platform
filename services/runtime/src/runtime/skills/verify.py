"""Skill 包验签与逐文件核哈希（S9.1/9.2，冻结契约 ①⑤）。

- ``verify_dir(path, lock_entry) -> Manifest``：校验已物化目录——拒符号链接 / 非常规文件；``manifest.json`` 必须是规范化
  JSON 字节；``manifest.sig``（``{"alg":"ES256","kid","sig"}``，DER、标准 base64）用 ``SKILL_TRUST_KEYS_FILE`` 中 kid 对应的
  公钥验签；``manifest_sha256`` 与名称 / 版本须与 Agent 快照的 ``skills_lock`` 条目一致；清单中每个文件路径须合规（拒绝绝对
  路径、``..``、反斜杠、空段），以 python 读取内容核对 size 与 sha256；目录中不得有清单之外的文件。
- ``verify_package(data, lock_entry) -> (Manifest, files)``：对网关下载的 tgz 在内存中做同样的校验（不落盘），成员只接受
  常规文件与目录（拒符号链接、硬链接、设备文件），供 ``store.materialize`` 在写盘前先验证。

本机透明加密（DLP）实测：python 解包写出的 .py 在磁盘上为密文，但经 python 读回与包内字节一致；因此哈希一律以 python
读取的明文计算（与包内字节一致），注入沙箱时同样由 python 读明文后以 tar 流经 ``docker cp`` 送入，不走绑定挂载。

失败一律抛 ``SkillVerifyError(code)``，code 为 ``SKILL_VERIFY_FAILED``（Run 以该错误码失败，失败关闭）。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import os
import stat
import tarfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

from runtime.skills import settings as skill_settings

SKILL_VERIFY_FAILED = "SKILL_VERIFY_FAILED"
SKILL_REVOKED = "SKILL_REVOKED"
SKILL_UNAVAILABLE = "SKILL_UNAVAILABLE"
MANIFEST_FILE = "manifest.json"
SIG_FILE = "manifest.sig"
SCHEMA = "ai-platform.skill-manifest/v1"
SANDBOX_LEVELS = ("none", "read-only", "workspace-write", "full-access")
_RESERVED = frozenset({MANIFEST_FILE, SIG_FILE})
_MAX_PATH = 255


class SkillVerifyError(Exception):
    """Skill 校验失败（code：SKILL_VERIFY_FAILED / SKILL_REVOKED / SKILL_UNAVAILABLE）。"""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message or code


def _fail(message: str) -> SkillVerifyError:
    return SkillVerifyError(SKILL_VERIFY_FAILED, message)


@dataclass(frozen=True)
class Manifest:
    """已验签的清单（字段见冻结契约 ①）。"""

    name: str
    version: str
    tenant: str
    files: tuple[dict[str, Any], ...]
    capabilities: tuple[str, ...]
    risk_level: str
    data_class: str
    sandbox: str
    sha256: str  # manifest_sha256 = sha256(规范化字节)
    kid: str
    raw: dict[str, Any] = field(default_factory=dict, compare=False)

    def file(self, path: str) -> dict[str, Any] | None:
        return next((f for f in self.files if f["path"] == path), None)


def canonical(obj: Any) -> bytes:
    """契约 ① 的规范化 JSON 字节。"""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _b64url(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def jwk_thumbprint(jwk: Mapping[str, Any]) -> str:
    """RFC 7638 指纹（EC 公钥：crv、kty、x、y）。"""
    members = {"crv": jwk["crv"], "kty": jwk["kty"], "x": jwk["x"], "y": jwk["y"]}
    digest = hashlib.sha256(json.dumps(members, sort_keys=True, separators=(",", ":")).encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def load_trust_keys(path: str | None = None) -> dict[str, ec.EllipticCurvePublicKey]:
    """读取信任公钥 JWKS → {kid: 公钥}。kid 一律按 RFC 7638 指纹计算；JWK 自带 kid 与指纹不一致的条目丢弃。"""
    path = path or skill_settings.load().trust_keys_file
    if not path:
        raise _fail("SKILL_TRUST_KEYS_FILE is not configured")
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise _fail(f"trust keys file unreadable: {type(e).__name__}") from e
    keys: dict[str, ec.EllipticCurvePublicKey] = {}
    for jwk in doc.get("keys", []) if isinstance(doc, dict) else []:
        try:
            if jwk.get("kty") != "EC" or jwk.get("crv") != "P-256":
                continue
            kid = jwk_thumbprint(jwk)
            if jwk.get("kid") not in (None, kid):
                continue
            x, y = (int.from_bytes(_b64url(jwk[k]), "big") for k in ("x", "y"))
            keys[kid] = ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1()).public_key()
        except (KeyError, TypeError, ValueError, binascii.Error):
            continue
    if not keys:
        raise _fail("no usable ES256 trust keys")
    return keys


def check_rel_path(p: Any) -> str:
    """包内相对路径合规：非空字符串、POSIX 分隔、非绝对、无 ``..`` / ``.`` / 空段、无反斜杠与 NUL、不超长。"""
    if not isinstance(p, str) or not p or len(p) > _MAX_PATH or "\\" in p or "\x00" in p:
        raise _fail(f"illegal path {p!r}")
    if p.startswith("/") or (len(p) > 1 and p[1] == ":"):
        raise _fail(f"absolute path {p!r}")
    parts = p.split("/")
    if any(x in ("", ".", "..") for x in parts):
        raise _fail(f"path traversal or empty segment in {p!r}")
    return p


def _parse_manifest(manifest_bytes: bytes, sig_bytes: bytes, lock_entry: Mapping[str, Any] | None) -> Manifest:
    try:
        obj = json.loads(manifest_bytes)
    except ValueError as e:
        raise _fail("manifest.json is not valid JSON") from e
    if not isinstance(obj, dict) or canonical(obj) != manifest_bytes:
        raise _fail("manifest.json is not in canonical form")
    try:
        sig = json.loads(sig_bytes)
        alg, kid, sig_b64 = sig["alg"], sig["kid"], sig["sig"]
    except (ValueError, KeyError, TypeError) as e:
        raise _fail("manifest.sig malformed") from e
    if alg != "ES256" or not isinstance(kid, str) or not isinstance(sig_b64, str):
        raise _fail(f"unsupported signature alg {alg!r}")
    key = load_trust_keys().get(kid)
    if key is None:
        raise _fail(f"signing key {kid!r} is not trusted")
    try:
        key.verify(base64.b64decode(sig_b64, validate=True), manifest_bytes, ec.ECDSA(hashes.SHA256()))
    except (InvalidSignature, binascii.Error, ValueError) as e:
        raise _fail("signature verification failed") from e
    sha = hashlib.sha256(manifest_bytes).hexdigest()
    if obj.get("schema") != SCHEMA:
        raise _fail(f"unsupported manifest schema {obj.get('schema')!r}")
    for k in ("name", "version", "tenant", "risk_level", "data_class", "sandbox"):
        if not isinstance(obj.get(k), str) or not obj[k]:
            raise _fail(f"manifest field {k!r} missing")
    if obj["sandbox"] not in SANDBOX_LEVELS:
        raise _fail(f"manifest sandbox {obj['sandbox']!r} invalid")
    caps = obj.get("capabilities")
    if not isinstance(caps, list) or not all(isinstance(c, str) for c in caps):
        raise _fail("manifest capabilities invalid")
    files = obj.get("files")
    if not isinstance(files, list):
        raise _fail("manifest files invalid")
    seen: set[str] = set()
    for f in files:
        if not isinstance(f, dict) or not isinstance(f.get("sha256"), str) or not isinstance(f.get("size"), int) or f["size"] < 0:
            raise _fail("manifest file entry invalid")
        p = check_rel_path(f.get("path"))
        if p in _RESERVED or p in seen:
            raise _fail(f"manifest file entry {p!r} duplicated or reserved")
        seen.add(p)
    if [f["path"] for f in files] != sorted(seen):
        raise _fail("manifest files are not sorted by path")
    if "SKILL.md" not in seen:
        raise _fail("SKILL.md missing from manifest")
    if lock_entry is not None:
        if obj["name"] != lock_entry.get("name") or obj["version"] != str(lock_entry.get("version")):
            raise _fail(f"manifest is {obj['name']}@{obj['version']}, lock expects {lock_entry.get('name')}@{lock_entry.get('version')}")
        if lock_entry.get("manifest_sha256") != sha:
            raise _fail("manifest_sha256 does not match skills_lock")
    return Manifest(
        name=obj["name"],
        version=obj["version"],
        tenant=obj["tenant"],
        files=tuple(files),
        capabilities=tuple(caps),
        risk_level=obj["risk_level"],
        data_class=obj["data_class"],
        sandbox=obj["sandbox"],
        sha256=sha,
        kid=kid,
        raw=obj,
    )


def check_file(manifest: Manifest, path: str, data: bytes) -> None:
    """单个文件的 size + sha256 与清单一致，否则抛 SKILL_VERIFY_FAILED。"""
    entry = manifest.file(path)
    if entry is None:
        raise _fail(f"file {path!r} is not in the manifest")
    if len(data) != entry["size"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
        raise _fail(f"hash mismatch for {path!r}")


def _scan_dir(root: Path) -> dict[str, Path]:
    """列出目录下全部文件（相对 POSIX 路径 → 绝对路径）；遇符号链接或非常规文件即失败。"""
    if root.is_symlink() or not root.is_dir():
        raise _fail(f"{root} is not a directory")
    out: dict[str, Path] = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for n in [*dirnames, *filenames]:
            p = Path(dirpath) / n
            mode = os.lstat(p).st_mode
            if stat.S_ISLNK(mode):
                raise _fail(f"symlink not allowed: {p.relative_to(root)}")
            if n in filenames:
                if not stat.S_ISREG(mode):
                    raise _fail(f"not a regular file: {p.relative_to(root)}")
                out[PurePosixPath(p.relative_to(root)).as_posix()] = p
            elif not stat.S_ISDIR(mode):
                raise _fail(f"not a directory: {p.relative_to(root)}")
    return out


def verify_dir(path: str | Path, lock_entry: Mapping[str, Any] | None) -> Manifest:
    """验证已物化的技能目录（见模块说明）。返回已验签的清单。"""
    root = Path(path)
    found = _scan_dir(root)
    if MANIFEST_FILE not in found or SIG_FILE not in found:
        raise _fail("manifest.json / manifest.sig missing")
    manifest = _parse_manifest(found[MANIFEST_FILE].read_bytes(), found[SIG_FILE].read_bytes(), lock_entry)
    listed = {f["path"] for f in manifest.files}
    extra = sorted(set(found) - listed - _RESERVED)
    if extra:
        raise _fail(f"files not in manifest: {', '.join(extra[:5])}")
    for p in sorted(listed):
        if p not in found:
            raise _fail(f"missing file {p!r}")
        check_file(manifest, p, found[p].read_bytes())  # python 读取：透明加密下得到明文
    return manifest


def verify_package(data: bytes, lock_entry: Mapping[str, Any] | None) -> tuple[Manifest, dict[str, bytes]]:
    """在内存中校验网关下载的 tgz（不落盘）。返回 (清单, {相对路径: 字节})，含 manifest.json 与 manifest.sig。"""
    st = skill_settings.load()
    files: dict[str, bytes] = {}
    total = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            for m in tar:
                name = m.name[2:] if m.name.startswith("./") else m.name
                if m.isdir():
                    if name not in ("", "."):
                        check_rel_path(name.rstrip("/"))
                    continue
                if not m.isreg():
                    raise _fail(f"package member {m.name!r} is not a regular file (links / devices are rejected)")
                check_rel_path(name)
                if name in files:
                    raise _fail(f"duplicate package member {name!r}")
                total += m.size
                if total > st.package_max_bytes or len(files) >= st.package_max_files:
                    raise _fail("package exceeds size / file-count limits")
                fh = tar.extractfile(m)
                if fh is None:
                    raise _fail(f"package member {name!r} unreadable")
                files[name] = fh.read()
    except (tarfile.TarError, OSError, EOFError) as e:
        raise _fail(f"package is not a valid tgz: {type(e).__name__}") from e
    if MANIFEST_FILE not in files or SIG_FILE not in files:
        raise _fail("manifest.json / manifest.sig missing from package")
    manifest = _parse_manifest(files[MANIFEST_FILE], files[SIG_FILE], lock_entry)
    listed = {f["path"] for f in manifest.files}
    extra = sorted(set(files) - listed - _RESERVED)
    if extra:
        raise _fail(f"files not in manifest: {', '.join(extra[:5])}")
    for p in sorted(listed):
        if p not in files:
            raise _fail(f"missing file {p!r}")
        check_file(manifest, p, files[p])
    return manifest, files
