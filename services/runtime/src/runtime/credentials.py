"""Run 发起人令牌的加密存储（S7 §4，D113）。

持久化执行的 Run 由 Worker 接管续跑，需要发起人令牌（工具经能力网关执行时换 OBO 令牌，D60）。令牌不进 Redis、
不进事件日志 / 日志 / repr，只以 AES-GCM 密文存 ``t_run_credential``：

- 密钥：``CREDENTIAL_ENC_KEY``（经 secret_store；生产基线要求经 Vault / 挂载文件注入且不是开发默认值），
  以 HKDF-SHA256 派生出 256 位 AES 密钥；
- AAD = run_id：密文绑定所属 Run，整行搬到另一个 Run 下解不开（防替换）；
- 格式：版本字节 0x01 + 12 字节随机 nonce + 密文（含 16 字节认证标签）。

只接受 Run 发起人的令牌（创建、续跑、/input 刷新时覆盖），不存 refresh token：过期即挂起为
input-required{kind:credential} 等发起人刷新。令牌 exp 只做不验签解析，用于剩余有效期判断（验签在 API 鉴权时已做）。
Run 进入终态即删除。
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from functools import lru_cache
from typing import NamedTuple

import jwt
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.config import settings
from runtime.state import db
from runtime.state.db import Run, RunCredential
from runtime.state.eventlog import RunNotFound

logger = logging.getLogger(__name__)

_FORMAT_V1 = b"\x01"
_NONCE_BYTES = 12
_HKDF_INFO = b"ai-runtime/run-credential/v1"


class CredentialSubjectMismatch(PermissionError):
    """令牌主体不是 Run 发起人：只接受发起人令牌（D87 修订 / D113）。"""


class StoredCredential(NamedTuple):
    """解密后的凭据（可按 ``(subject, token, expires_at)`` 解包）。repr 不含令牌。"""

    subject: str
    token: str
    expires_at: datetime | None

    def __repr__(self) -> str:
        return f"StoredCredential(subject={self.subject!r}, token=<redacted>, expires_at={self.expires_at!r})"


@lru_cache(maxsize=4)
def _aead(material: str) -> AESGCM:
    """由密钥材料派生 AES-256-GCM 实例（按材料缓存；材料只在内存中）。"""
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_HKDF_INFO).derive(material.encode("utf-8"))
    return AESGCM(key)


def encrypt(run_id: str, token: str) -> bytes:
    """以 AAD=run_id 加密令牌。"""
    nonce = os.urandom(_NONCE_BYTES)
    return _FORMAT_V1 + nonce + _aead(settings.credential_enc_key).encrypt(nonce, token.encode("utf-8"), run_id.encode("utf-8"))


def decrypt(run_id: str, blob: bytes) -> str:
    """解密；格式不符、密钥不对或 AAD（run_id）不符抛 ValueError。"""
    if len(blob) <= 1 + _NONCE_BYTES or blob[:1] != _FORMAT_V1:
        raise ValueError("unsupported credential ciphertext format")
    nonce, body = blob[1 : 1 + _NONCE_BYTES], blob[1 + _NONCE_BYTES :]
    try:
        plain = _aead(settings.credential_enc_key).decrypt(nonce, body, run_id.encode("utf-8"))
    except InvalidTag as exc:
        raise ValueError("credential ciphertext does not authenticate for this run") from exc
    return plain.decode("utf-8")


def token_expiry(token: str) -> datetime | None:
    """不验签解析令牌的 exp（只用于剩余有效期判断）；不是 JWT 或没有数值 exp 时返回 None。"""
    try:
        claims = jwt.decode(
            token,
            options={
                "verify_signature": False,
                "verify_exp": False,
                "verify_nbf": False,
                "verify_iat": False,
                "verify_aud": False,
                "verify_iss": False,
            },
        )
    except jwt.PyJWTError:
        return None
    exp = claims.get("exp")
    if isinstance(exp, bool) or not isinstance(exp, int | float):
        return None
    try:
        return datetime.fromtimestamp(exp, UTC)
    except (OverflowError, OSError, ValueError):
        return None


def remaining_s(expires_at: datetime | None, now: datetime | None = None) -> float:
    """剩余有效期（秒）；无到期时间视为无限。"""
    if expires_at is None:
        return float("inf")
    return (expires_at - (now or datetime.now(UTC))).total_seconds()


def is_fresh(expires_at: datetime | None, min_ttl_s: float | None = None) -> bool:
    """剩余有效期是否不低于 CREDENTIAL_MIN_TTL_S（或给定阈值）。"""
    return remaining_s(expires_at) >= (settings.credential_min_ttl_s if min_ttl_s is None else min_ttl_s)


async def store_credential(
    session: AsyncSession, run_id: str, subject: str, token: str, expires_at: datetime | None = None
) -> datetime | None:
    """在调用方事务内写入（覆盖）该 Run 的发起人令牌密文，返回记录的到期时间（未给出时取令牌 exp）。
    subject 不是 Run 发起人抛 CredentialSubjectMismatch；Run 不存在抛 RunNotFound。调用方负责提交。"""
    run = await session.get(Run, run_id)
    if run is None:
        raise RunNotFound(run_id)
    if subject != run.subject:
        raise CredentialSubjectMismatch(f"run {run_id}: only the run initiator's credential is accepted")
    exp = expires_at if expires_at is not None else token_expiry(token)
    blob = encrypt(run_id, token)
    row = await session.get(RunCredential, run_id)
    if row is None:
        session.add(RunCredential(run_id=run_id, subject=subject, ciphertext=blob, expires_at=exp))
    else:
        row.subject, row.ciphertext, row.expires_at = subject, blob, exp
    await session.flush()
    return exp


async def load_credential(run_id: str) -> StoredCredential | None:
    """读取并解密该 Run 的凭据；不存在或无法解密（密钥轮换、密文被替换）返回 None（调用方转 credential 输入）。"""
    async with db.SessionLocal() as s:
        row = await s.get(RunCredential, run_id)
    if row is None:
        return None
    try:
        token = decrypt(run_id, row.ciphertext)
    except ValueError:
        logger.warning("stored credential for run %s cannot be decrypted; treating as missing", run_id)
        return None
    return StoredCredential(row.subject, token, row.expires_at)


async def delete_credential(run_id: str, session: AsyncSession | None = None) -> bool:
    """删除该 Run 的凭据（Run 进入终态时调用）。给定 session 时在其事务内执行、由调用方提交；否则自开会话并提交。"""
    stmt = delete(RunCredential).where(RunCredential.run_id == run_id)
    if session is not None:
        res = await session.execute(stmt)
        return bool(getattr(res, "rowcount", 0))
    async with db.SessionLocal() as s:
        res = await s.execute(stmt)
        await s.commit()
    return bool(getattr(res, "rowcount", 0))
