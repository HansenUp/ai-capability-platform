"""S7 WP-C：发起人凭据的加密存储与有效期（§4，D113）。

- AES-GCM 加解密、AAD=run_id 防替换、只接受发起人主体、令牌不进事件日志 / repr；
- drive_durable：凭据剩余有效期不足 / 缺失（强制鉴权时）→ INPUT_REQUESTED{kind:credential}、input-required；
  provide_input(credential) 刷新后续跑完成，Run 终态即删凭据；
- 写工具执行前（TOOL_EXECUTING 前）令牌将过期 → 挂起而不是推入结果未知；
- 生产基线要求 CREDENTIAL_ENC_KEY。
"""

from __future__ import annotations

import dataclasses
import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest

from ai_contract import DoneEvent, InputRequiredEvent, RunEventType, RunState
from runtime import config, credentials
from runtime.loop import runner, user_input
from runtime.run import invariant
from runtime.state import db
from runtime.state.db import AgentVersion, RunCredential, RunInput
from runtime.state.eventlog import EventLog

SUBJECT = "u-cred"
_KEY = "test-signing-key-0123456789abcdef"  # noqa: S105 —— 测试签名密钥（不验签，只用于构造 exp）


def _token(ttl_s: float, sub: str = SUBJECT) -> str:
    return jwt.encode({"sub": sub, "exp": int(time.time() + ttl_s), "nonce": uuid.uuid4().hex}, _KEY, algorithm="HS256")


async def _agent(config_: dict[str, Any]) -> AgentVersion:
    av = AgentVersion(agent_id=f"s7k-{uuid.uuid4().hex[:8]}", version="1", status="published", system_prompt="x", config=config_)
    async with db.SessionLocal() as s:
        s.add(av)
        await s.commit()
    return av


async def _run(agent: AgentVersion, content: str = "hi", token: str | None = None) -> str:
    msgs = [{"role": "user", "content": content}]
    rid = await runner.create_run(agent, SUBJECT, None, initial_messages=msgs)
    async with db.SessionLocal() as s:
        s.add(RunInput(run_id=rid, messages=msgs, fingerprint=invariant.message_hash(msgs)))
        if token is not None:
            await credentials.store_credential(s, rid, SUBJECT, token)
        await s.commit()
    return rid


async def _log_text(rid: str) -> str:
    async with db.SessionLocal() as s:
        return json.dumps([e.model_dump(mode="json") for e in await EventLog(s).list(rid)], ensure_ascii=False)


async def _state(rid: str) -> str:
    async with db.SessionLocal() as s:
        run = await s.get(db.Run, rid)
    assert run is not None
    return run.state


def test_encrypt_roundtrip_and_aad_binding() -> None:
    blob = credentials.encrypt("run-a", "secret-token-value")
    assert b"secret-token-value" not in blob and blob[:1] == b"\x01"
    assert credentials.decrypt("run-a", blob) == "secret-token-value"
    with pytest.raises(ValueError):
        credentials.decrypt("run-b", blob)  # AAD=run_id：换到别的 Run 解不开
    with pytest.raises(ValueError):
        credentials.decrypt("run-a", blob[:-1] + bytes([blob[-1] ^ 1]))
    assert credentials.encrypt("run-a", "x") != credentials.encrypt("run-a", "x")  # 随机 nonce


def test_token_expiry_parsing_and_repr() -> None:
    exp = credentials.token_expiry(_token(3600))
    assert exp is not None and 3500 < credentials.remaining_s(exp) <= 3600
    assert credentials.token_expiry("not-a-jwt") is None
    assert credentials.token_expiry(jwt.encode({"sub": "x"}, _KEY, algorithm="HS256")) is None
    assert credentials.is_fresh(None)
    sc = credentials.StoredCredential("u", "tok-SECRET", None)
    sub, tok, _ = sc
    assert tok == "tok-SECRET" and "tok-SECRET" not in repr(sc)


async def test_store_load_subject_check_and_aad_swap() -> None:
    agent = await _agent({"tools": ["echo"]})
    tok = _token(3600)
    a = await _run(agent, token=tok)
    b = await _run(agent)
    loaded = await credentials.load_credential(a)
    assert loaded is not None and loaded.token == tok and loaded.subject == SUBJECT and loaded.expires_at is not None
    async with db.SessionLocal() as s:
        with pytest.raises(credentials.CredentialSubjectMismatch):
            await credentials.store_credential(s, b, "approver-x", _token(3600, "approver-x"))
        row_a = await s.get(RunCredential, a)
        assert row_a is not None and tok.encode() not in row_a.ciphertext
        s.add(RunCredential(run_id=b, subject=SUBJECT, ciphertext=row_a.ciphertext, expires_at=row_a.expires_at))
        await s.commit()
    assert await credentials.load_credential(b) is None  # 整行搬到另一个 Run：认证失败，视为缺失
    assert await credentials.delete_credential(a) is True
    assert await credentials.load_credential(a) is None


async def test_expiring_credential_suspends_then_refresh_completes_and_deletes() -> None:
    agent = await _agent({"tools": ["echo"]})
    old = _token(30)  # 低于 CREDENTIAL_MIN_TTL_S（60）
    rid = await _run(agent, token=old)
    evs = [ev async for ev in runner.drive_durable(rid)]
    asked = [e for e in evs if isinstance(e, InputRequiredEvent)]
    assert asked and isinstance(evs[-1], DoneEvent) and evs[-1].stop_reason == "awaiting"
    assert await _state(rid) == RunState.INPUT_REQUIRED
    async with db.SessionLocal() as s:
        log = await EventLog(s).list(rid)
    req = [e.payload for e in log if e.type == RunEventType.INPUT_REQUESTED]
    assert req == [{"kind": "credential", "call_id": None, "reason": "expiring"}]
    assert not [e for e in log if e.type == RunEventType.MODEL_REQUEST]

    with pytest.raises(user_input.InputForbidden):
        await user_input.provide_input(rid, "other", "credential", value=_token(3600, "other"))
    with pytest.raises(ValueError, match="expires"):
        await user_input.provide_input(rid, SUBJECT, "credential", value=_token(10))
    with pytest.raises(ValueError, match="no fresh credential"):
        await user_input.provide_input(rid, SUBJECT, "credential")  # API 层未存入新令牌
    new = _token(3600)
    await user_input.provide_input(rid, SUBJECT, "credential", value=new)
    loaded = await credentials.load_credential(rid)
    assert loaded is not None and loaded.token == new

    evs = [ev async for ev in runner.drive_durable(rid)]
    assert isinstance(evs[-1], DoneEvent) and evs[-1].stop_reason == "end_turn"
    assert await _state(rid) == RunState.COMPLETED
    assert await credentials.load_credential(rid) is None  # 终态即删
    text = await _log_text(rid)
    assert old not in text and new not in text  # 令牌不进事件日志


async def test_missing_credential_with_auth_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "settings", dataclasses.replace(runner.settings, auth_required=True))
    agent = await _agent({"tools": ["echo"]})
    rid = await _run(agent)
    [ev async for ev in runner.drive_durable(rid)]
    async with db.SessionLocal() as s:
        log = await EventLog(s).list(rid)
    assert [e.payload["reason"] for e in log if e.type == RunEventType.INPUT_REQUESTED] == ["missing"]
    assert await _state(rid) == RunState.INPUT_REQUIRED


async def test_credential_checked_before_tool_executing() -> None:
    """令牌在 drive 中途临近过期：写工具执行前挂起为 credential 输入（不写 TOOL_EXECUTING），刷新后补完该调用。"""
    agent = await _agent({"tools": ["echo"]})
    msgs = [{"role": "user", "content": '!tool echo {"text": "hi"}'}]
    rid = await _run(agent, msgs[0]["content"])
    soon = datetime.now(UTC) + timedelta(seconds=20)
    evs = [ev async for ev in runner.drive(rid, agent, SUBJECT, msgs, credential="t", credential_expires_at=soon)]
    asked = [e for e in evs if isinstance(e, InputRequiredEvent)]
    assert asked and asked[0].call_id
    async with db.SessionLocal() as s:
        log = await EventLog(s).list(rid)
    assert not [e for e in log if e.type == RunEventType.TOOL_EXECUTING]
    assert [e.payload for e in log if e.type == RunEventType.INPUT_REQUESTED] == [
        {"kind": "credential", "call_id": asked[0].call_id, "reason": "expiring"}
    ]
    await user_input.provide_input(rid, SUBJECT, "credential", value=_token(3600))
    evs = [ev async for ev in runner.drive_durable(rid)]
    assert isinstance(evs[-1], DoneEvent) and evs[-1].stop_reason == "end_turn"
    async with db.SessionLocal() as s:
        log = await EventLog(s).list(rid)
    assert sum(1 for e in log if e.type == RunEventType.TOOL_EXECUTING) == 1


def test_production_baseline_requires_credential_key() -> None:
    prod = dataclasses.replace(config.settings, runtime_env="prod")
    assert "CREDENTIAL_ENC_KEY 未配置或仍为开发默认值" in config.production_problems(prod)
    assert "CREDENTIAL_ENC_KEY 未配置或仍为开发默认值" not in config.production_problems(
        dataclasses.replace(prod, credential_enc_key="k-real")
    )
    assert "k-real" not in repr(dataclasses.replace(prod, credential_enc_key="k-real"))
