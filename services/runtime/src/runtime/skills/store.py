"""Skill 物化与吊销列表（S9.5，D144；冻结契约 ⑤）。

- ``materialize(name, version, manifest_sha256) -> Path``：吊销检查 → 本地已有且校验通过即复用 → 否则经能力网关
  ``GET /skills/{n}/versions/{v}/package``（服务令牌，契约 ②）下载 → 内存中验签核哈希 → 写入 ``SKILLS_DIR`` 下的临时目录 →
  从磁盘再校验一遍（确认落盘后经 python 读回的字节与包内一致）→ 原子 rename 到 ``SKILLS_DIR/<n>/<v>``。同一技能版本的并发
  物化按进程内锁串行；目标目录已存在但校验失败（被篡改 / 残缺）时整体替换。
- ``load_locked(lock, tenant)``：Agent 快照 ``skills_lock`` → 已验签的 SkillDir 列表（缺包懒拉取）；清单租户须与 Agent 一致。
- ``revocations``：吊销列表缓存，随节点心跳（runtime.node.agent）与按 ``SKILL_REVOCATION_TTL_S`` 定期拉取
  ``GET /skills/revocations``；命中抛 ``SKILL_REVOKED``。刷新连续失败超过 ``SKILL_REVOCATION_MAX_STALE_S`` 即失败关闭。
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
import time
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx

from ai_contract import SkillDir
from runtime import gateway_client, net
from runtime.config import settings
from runtime.skills import settings as skill_settings
from runtime.skills.verify import (
    MANIFEST_FILE,
    SKILL_REVOKED,
    SKILL_UNAVAILABLE,
    SKILL_VERIFY_FAILED,
    Manifest,
    SkillVerifyError,
    verify_dir,
    verify_package,
)

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")  # Agent Skills 规范：小写字母、数字、连字符
VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")

_locks: dict[tuple[int, str, str], asyncio.Lock] = {}
_verified: dict[Path, Manifest] = {}  # 本进程内已验签的目录 → 清单（供平台工具读取，工具内仍逐文件核哈希）


def base_dir() -> Path:
    return Path(skill_settings.load().skills_dir).resolve()


def skill_path(name: str, version: str) -> Path:
    if not NAME_RE.fullmatch(name) or not VERSION_RE.fullmatch(version) or ".." in version:
        raise SkillVerifyError(SKILL_VERIFY_FAILED, f"illegal skill name / version {name!r}@{version!r}")
    return base_dir() / name / version


def verified_manifest(path: Path) -> Manifest | None:
    return _verified.get(Path(path).resolve())


def _lock_for(name: str, version: str) -> asyncio.Lock:
    key = (id(asyncio.get_running_loop()), name, version)
    lock = _locks.get(key)
    if lock is None:
        lock = _locks[key] = asyncio.Lock()
    return lock


# ---- 吊销列表 ----
class Revocations:
    """吊销列表缓存：{(name, version): manifest_sha256}。"""

    def __init__(self) -> None:
        self.entries: dict[tuple[str, str], str] = {}
        self.fetched_at: float | None = None  # monotonic；None 表示从未成功拉取
        self.generated_at: str | None = None

    def reset(self) -> None:
        self.entries, self.fetched_at, self.generated_at = {}, None, None

    def apply(self, body: Mapping[str, Any]) -> None:
        """按契约 ② 的响应体替换缓存（节点代理或测试也可直接喂入）。"""
        revoked = body.get("revoked")
        if not isinstance(revoked, list):
            raise ValueError("revocations body malformed")
        entries: dict[tuple[str, str], str] = {}
        for r in revoked:
            if isinstance(r, dict) and isinstance(r.get("name"), str) and r.get("version") is not None:
                entries[(r["name"], str(r["version"]))] = str(r.get("manifest_sha256") or "")
        self.entries, self.fetched_at, self.generated_at = entries, time.monotonic(), body.get("generated_at")

    async def refresh(self) -> bool:
        """拉取一次吊销列表；成功返回 True（失败保留旧列表）。"""
        try:
            hdrs = await gateway_client.headers(None)
            url = f"{settings.capability_gateway_url.rstrip('/')}/skills/revocations"
            async with net.client(timeout=skill_settings.load().fetch_timeout_s, transport=gateway_client.transport()) as c:
                r = await c.get(url, headers=hdrs)
            if r.status_code != 200:
                return False
            self.apply(r.json())
            return True
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            return False

    async def ensure_fresh(self) -> None:
        """过期即刷新；刷新失败且旧列表超过最大沿用时长（或从未拉取成功）→ 失败关闭。"""
        st = skill_settings.load()
        now = time.monotonic()
        if self.fetched_at is not None and now - self.fetched_at < st.revocation_ttl_s:
            return
        if await self.refresh():
            return
        if self.fetched_at is None or now - self.fetched_at > st.revocation_max_stale_s:
            raise SkillVerifyError(SKILL_VERIFY_FAILED, "skill revocation list unavailable (fail closed)")

    def is_revoked(self, name: str, version: str) -> bool:
        return (name, str(version)) in self.entries


revocations = Revocations()


async def check_not_revoked(name: str, version: str) -> None:
    await revocations.ensure_fresh()
    if revocations.is_revoked(name, version):
        raise SkillVerifyError(SKILL_REVOKED, f"skill {name}@{version} has been revoked")


# ---- 下载与物化 ----
async def _download(name: str, version: str) -> bytes:
    st = skill_settings.load()
    url = f"{settings.capability_gateway_url.rstrip('/')}/skills/{name}/versions/{version}/package"
    try:
        hdrs = await gateway_client.headers(None)
        async with net.client(timeout=st.fetch_timeout_s, transport=gateway_client.transport()) as c:
            async with c.stream("GET", url, headers=hdrs) as r:
                if r.status_code != 200:
                    raise SkillVerifyError(SKILL_UNAVAILABLE, f"gateway returned HTTP {r.status_code} for {name}@{version} package")
                buf = bytearray()
                async for chunk in r.aiter_bytes():
                    buf += chunk
                    if len(buf) > st.package_max_bytes:
                        raise SkillVerifyError(SKILL_VERIFY_FAILED, "package exceeds SKILL_PACKAGE_MAX_BYTES")
                return bytes(buf)
    except httpx.HTTPError as e:
        code = getattr(e, "code", None)
        raise SkillVerifyError(SKILL_UNAVAILABLE, f"package download failed: {code or type(e).__name__}") from e


def _write_tree(root: Path, files: Mapping[str, bytes]) -> None:
    for rel, data in files.items():
        p = root.joinpath(*rel.split("/"))
        p.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        with open(p, "wb") as fh:  # python 写入：透明加密下磁盘为密文，python 读回为明文（verify_dir 复核）
            fh.write(data)
        os.chmod(p, 0o644)


def _install(dest: Path, files: Mapping[str, bytes], lock_entry: Mapping[str, Any]) -> Manifest:
    """写临时目录 → 磁盘复核 → 原子 rename（目标已存在则先移走旧目录再换入）。"""
    dest.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    tmp = Path(tempfile.mkdtemp(prefix=f".tmp-{dest.name}-", dir=dest.parent))
    trash: Path | None = None
    try:
        os.chmod(tmp, 0o755)  # noqa: S103 —— 技能目录须对沙箱 / 其他运行用户可读（内容只读、已验签）
        _write_tree(tmp, files)
        manifest = verify_dir(tmp, lock_entry)
        if dest.exists() or dest.is_symlink():
            trash = dest.parent / f".old-{dest.name}-{uuid.uuid4().hex[:8]}"
            os.rename(dest, trash)
        os.rename(tmp, dest)
        return manifest
    finally:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        if trash is not None:
            if trash.is_symlink():
                trash.unlink()
            else:
                shutil.rmtree(trash, ignore_errors=True)


def _verify_existing(dest: Path, lock_entry: Mapping[str, Any]) -> Manifest | None:
    if not (dest.exists() or dest.is_symlink()):
        return None
    try:
        return verify_dir(dest, lock_entry)
    except SkillVerifyError:
        return None  # 被篡改 / 残缺：重新下载并整体替换


async def materialize(name: str, version: str, manifest_sha256: str) -> Path:
    """下载 → 验签 → 临时目录 → 原子 rename 到 ``SKILLS_DIR/<n>/<v>``（见模块说明）；返回技能目录。"""
    dest = skill_path(name, version)
    lock_entry = {"name": name, "version": version, "manifest_sha256": manifest_sha256}
    await check_not_revoked(name, version)
    async with _lock_for(name, version):
        manifest = await asyncio.to_thread(_verify_existing, dest, lock_entry)
        if manifest is None:
            data = await _download(name, version)
            _, files = await asyncio.to_thread(verify_package, data, lock_entry)
            manifest = await asyncio.to_thread(_install, dest, files, lock_entry)
        _verified[dest.resolve()] = manifest
    return dest


async def load_locked(lock: Sequence[Mapping[str, Any]], tenant: str) -> list[SkillDir]:
    """按 skills_lock 加载（快照声明顺序）：吊销检查 → 本地校验，缺包或校验失败则懒拉取；清单租户须与 Agent 一致。"""
    out: list[SkillDir] = []
    for entry in lock:
        try:
            name, version, sha = str(entry["name"]), str(entry["version"]), str(entry["manifest_sha256"])
        except (KeyError, TypeError) as e:
            raise SkillVerifyError(SKILL_VERIFY_FAILED, "skills_lock entry malformed") from e
        path = await materialize(name, version, sha)
        manifest = _verified[path.resolve()]
        if manifest.tenant != tenant:
            raise SkillVerifyError(SKILL_VERIFY_FAILED, f"skill {name}@{version} belongs to tenant {manifest.tenant!r}")
        out.append(
            SkillDir(name=name, version=version, path=path, signature=f"sha256:{sha}", capabilities_declared=list(manifest.capabilities))
        )
    return out


def lock_entry_of(skill: SkillDir) -> dict[str, str] | None:
    """SkillDir → lock 条目（load_locked 产出的 SkillDir 在 signature 中带 manifest_sha256）；旧式开发技能返回 None。"""
    sig = skill.signature or ""
    if not sig.startswith("sha256:"):
        return None
    return {"name": skill.name, "version": skill.version, "manifest_sha256": sig.removeprefix("sha256:")}


def manifest_of(skill: SkillDir) -> Manifest | None:
    """已验签技能的清单：优先取本进程缓存，否则按 lock 条目重新校验目录；旧式开发技能返回 None。"""
    entry = lock_entry_of(skill)
    if entry is None:
        return None
    cached = verified_manifest(skill.path)
    if cached is not None and cached.sha256 == entry["manifest_sha256"]:
        return cached
    manifest = verify_dir(skill.path, entry)
    _verified[Path(skill.path).resolve()] = manifest
    return manifest


__all__ = [
    "MANIFEST_FILE",
    "Revocations",
    "base_dir",
    "check_not_revoked",
    "load_locked",
    "lock_entry_of",
    "manifest_of",
    "materialize",
    "revocations",
    "skill_path",
]
