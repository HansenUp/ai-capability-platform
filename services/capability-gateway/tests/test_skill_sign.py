"""Skill 签名（契约 ①）：规范化、ES256 签名 / 验签往返（与 cosign sign-blob 同格式的结构断言）、确定性 tgz、安全解包、密钥加载。"""

from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import stat
import tarfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from skill_testkit import SKILL_KEY, files

from capability_gateway import config, skill_sign
from capability_gateway.skill_sign import PackageError


def test_canonical_bytes_and_manifest_sha() -> None:
    obj = {"b": "工单", "a": [1, {"y": 2, "x": 1}]}
    assert skill_sign.canonical(obj) == '{"a":[1,{"x":1,"y":2}],"b":"工单"}'.encode()
    assert skill_sign.manifest_sha256(obj) == hashlib.sha256(skill_sign.canonical(obj)).hexdigest()


def test_file_entries_sorted_and_exclude_manifest() -> None:
    fs = {"b.md": b"b", "SKILL.md": b"s", "manifest.json": b"{}", "manifest.sig": b"{}"}
    entries = skill_sign.file_entries(fs)
    assert [e["path"] for e in entries] == ["SKILL.md", "b.md"]
    assert entries[0] == {"path": "SKILL.md", "sha256": hashlib.sha256(b"s").hexdigest(), "size": 1}


def test_sign_verify_roundtrip_cosign_compatible() -> None:
    body = skill_sign.canonical({"schema": skill_sign.SCHEMA, "name": "x"})
    sig = skill_sign.sign(body)
    assert set(sig) == {"alg", "kid", "sig"} and sig["alg"] == "ES256"
    der = base64.b64decode(sig["sig"], validate=True)  # 标准 base64（cosign sign-blob 输出同格式）
    r, s = decode_dss_signature(der)  # DER 编码的 ECDSA (r, s)
    assert r > 0 and s > 0
    # cosign verify-blob --key：ECDSA P-256 / SHA-256 直接对 blob 验签，公钥为 SubjectPublicKeyInfo PEM
    pub = serialization.load_pem_public_key(skill_sign.public_pem())
    assert isinstance(pub, ec.EllipticCurvePublicKey)
    pub.verify(der, body, ec.ECDSA(hashes.SHA256()))
    trust = skill_sign.jwks()
    (jwk,) = trust["keys"]
    thumb_src = json.dumps({k: jwk[k] for k in ("crv", "kty", "x", "y")}, separators=(",", ":"), sort_keys=True).encode()
    assert jwk["kid"] == sig["kid"] == base64.urlsafe_b64encode(hashlib.sha256(thumb_src).digest()).rstrip(b"=").decode()  # RFC 7638
    assert skill_sign.verify(body, sig, trust)
    assert not skill_sign.verify(body + b" ", sig, trust)  # 篡改
    assert not skill_sign.verify(body, sig | {"kid": "other"}, trust)  # 未知 kid
    other = ec.generate_private_key(ec.SECP256R1())
    assert not skill_sign.verify(body, skill_sign.sign(body, other), trust)  # 换签名密钥
    assert not skill_sign.verify(body, sig | {"alg": "none"}, trust)


def test_deterministic_tgz() -> None:
    fs = files() | {"scripts/run.py": b"print(1)\n"}
    a = skill_sign.build_tgz(fs)
    b = skill_sign.build_tgz(dict(reversed(list(fs.items()))))
    assert a == b
    assert gzip.decompress(a)  # 合法 gzip
    assert a[4:8] == b"\x00\x00\x00\x00"  # gzip 头 mtime=0
    with tarfile.open(fileobj=io.BytesIO(a), mode="r:gz") as tar:
        members = tar.getmembers()
    assert [m.name for m in members] == sorted(fs)
    for m in members:
        assert m.mtime == 0 and m.uid == 0 and m.gid == 0 and m.uname == "" and m.gname == "" and m.isreg()
        assert m.mode == (0o755 if m.name.startswith("scripts/") else 0o644)
    assert skill_sign.read_tgz(a) == fs


def _raw_tgz(add) -> bytes:  # type: ignore[no-untyped-def]
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        add(tar)
    return buf.getvalue()


def _file(tar: tarfile.TarFile, name: str, data: bytes = b"x") -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    tar.addfile(info, io.BytesIO(data))


@pytest.mark.parametrize("name", ["/etc/passwd", "../evil.md", "a/../../b.md", "a\\b.md"])
def test_read_tgz_rejects_unsafe_paths(name: str) -> None:
    with pytest.raises(PackageError) as e:
        skill_sign.read_tgz(_raw_tgz(lambda t: _file(t, name)))
    assert e.value.rule == "structure.archive"


def test_read_tgz_rejects_links_duplicates_and_garbage() -> None:
    def sym(t: tarfile.TarFile) -> None:
        _file(t, "SKILL.md")
        info = tarfile.TarInfo("references/leak.md")
        info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
        t.addfile(info)

    def dup(t: tarfile.TarFile) -> None:
        _file(t, "SKILL.md")
        _file(t, "./SKILL.md")

    for bad in (_raw_tgz(sym), _raw_tgz(dup), b"not a tgz"):
        with pytest.raises(PackageError) as e:
            skill_sign.read_tgz(bad)
        assert e.value.rule == "structure.archive"
    with pytest.raises(PackageError) as e:
        skill_sign.read_tgz(_raw_tgz(lambda t: [_file(t, f"references/{i}.md") for i in range(3)]), max_files=2)
    assert e.value.rule == "structure.size_total"


def test_read_tgz_strips_single_top_directory() -> None:
    data = _raw_tgz(lambda t: (_file(t, "demo/SKILL.md", b"s"), _file(t, "demo/evals/cases.jsonl", b"c")))
    assert skill_sign.read_tgz(data) == {"SKILL.md": b"s", "evals/cases.jsonl": b"c"}


def test_dev_key_generated_with_0600(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "keys" / "skill-signing.pem"
    monkeypatch.setattr(config, "SKILL_SIGNING_KEY_FILE", None)
    monkeypatch.setattr(config, "SKILL_DEV_KEY_FILE", str(path))
    skill_sign.reset_for_tests(None)
    try:
        key = skill_sign.load_key()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600 and stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        skill_sign.reset_for_tests(None)
        assert skill_sign.kid_of(skill_sign.load_key()) == skill_sign.kid_of(key)  # 再次启动沿用同一把
        monkeypatch.setattr(config, "GATEWAY_ENV", "prod")
        skill_sign.reset_for_tests(None)
        with pytest.raises(RuntimeError, match="SKILL_SIGNING_KEY_FILE"):
            skill_sign.load_key()
    finally:
        skill_sign.reset_for_tests(SKILL_KEY)


def test_configured_key_must_be_p256(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bad = tmp_path / "p384.pem"
    k = ec.generate_private_key(ec.SECP384R1())
    bad.write_bytes(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    monkeypatch.setattr(config, "SKILL_SIGNING_KEY_FILE", str(bad))
    skill_sign.reset_for_tests(None)
    try:
        with pytest.raises(RuntimeError, match="P-256"):
            skill_sign.load_key()
    finally:
        skill_sign.reset_for_tests(SKILL_KEY)


def test_cli_exports_jwks_and_pem(tmp_path: Path) -> None:
    out = tmp_path / "trust.json"
    assert skill_sign._cli(["jwks", "--out", str(out)]) == 0
    assert json.loads(out.read_text())["keys"][0]["kid"] == skill_sign.kid_of(SKILL_KEY)
    pem = tmp_path / "pub.pem"
    assert skill_sign._cli(["pem", "--out", str(pem)]) == 0
    assert pem.read_bytes().startswith(b"-----BEGIN PUBLIC KEY-----")
