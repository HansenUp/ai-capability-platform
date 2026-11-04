"""Disposable SQLite backup/restore acceptance for the real core Runtime schema.

This is an offline verification tool, not a backup/restore API. It cannot accept
an existing database, start a Worker, execute an attempt or unlock a subject.
The copied checkpoint is deliberately shown to be stale after a later write;
external queue reconciliation and rollback-resistant authority are not proven.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import runtime.api.isolated_a2a as isolated_a2a
import runtime.api.workbench as workbench
from runtime.api.auth import Principal
from runtime.state import db
from runtime.state.eventlog import EventLog


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_owned_root(root: Path) -> None:
    if root.is_symlink() or not root.is_dir() or any(root.iterdir()) or root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077:
        raise ValueError("Empty private owned temporary directory required")


def wal_present(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 0


def backup_snapshot(source: Path, snapshot: Path) -> str:
    if snapshot.exists() or snapshot.is_symlink():
        raise ValueError("Refuse to overwrite a snapshot")
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as reader:
        with closing(sqlite3.connect(snapshot)) as writer:
            reader.backup(writer)
            writer.commit()
            if writer.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise ValueError("Snapshot integrity check failed")
    return file_digest(snapshot)


def restore_snapshot(snapshot: Path, expected_digest: str, destination: Path) -> None:
    if file_digest(snapshot) != expected_digest:
        raise ValueError("Snapshot digest mismatch")
    if destination.exists() or destination.is_symlink():
        raise ValueError("Refuse to overwrite a restored database")
    with closing(sqlite3.connect(snapshot.as_uri() + "?mode=ro", uri=True)) as reader:
        with closing(sqlite3.connect(destination)) as writer:
            reader.backup(writer)
            writer.commit()
            if writer.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise ValueError("Restored database integrity check failed")


def fixture_principal(label: str, tenant: str = "restore-only") -> Principal:
    return Principal(
        subject="human:restore-" + label,
        tenant_id=tenant,
        auth_method="user",
        client_id="restore-fixture",
        actor="app:restore-fixture",
        token=uuid4().hex,
    )


async def seed(session: AsyncSession, principal: Principal, status: str) -> str:
    attempt_id, run_id = str(uuid4()), "restore_" + uuid4().hex[:20]
    scope_id = workbench.user_scope(principal)
    digest = hashlib.sha256(attempt_id.encode()).hexdigest()
    session.add(db.WorkbenchScope(scope_id=scope_id, active_attempt=attempt_id, revision=1))
    session.add(
        db.WorkbenchAttempt(
            attempt_id=attempt_id,
            scope_id=scope_id,
            run_id=run_id,
            action="create",
            fingerprint=digest,
            request_seq=0,
            status=status,
            revision=2 if status != "PREPARED" else 1,
            acknowledged=False,
            rejection="VALIDATION_REJECTED" if status == "REJECTED_NO_EFFECT" else None,
            evidence_seq=1 if status == "COMMITTED" else None,
            value_hmac=digest,
            create_agent="restore-only-agent",
            create_version="1",
            create_model="synthetic-model",
            create_snapshot=digest,
            create_reason="pinned",
        )
    )
    if status == "COMMITTED":
        session.add(
            db.Run(
                run_id=run_id,
                agent_id="restore-only-agent",
                agent_version="1",
                model_alias="synthetic-model",
                tenant_id=principal.tenant_id,
                subject=principal.subject,
                state="completed",
                last_seq=4,
                durable=False,
            )
        )
        payloads = [
            (
                "run.created",
                {
                    "workbench_attempt_id": attempt_id,
                    "workbench_snapshot_hash": digest,
                    "workbench_value_hmac": digest,
                    "subject": principal.subject,
                    "agent_id": "restore-only-agent",
                    "agent_version": "1",
                    "model_alias": "synthetic-model",
                },
            ),
            ("model.response", {"text": "public synthetic restored result"}),
            ("run.done", {"stop_reason": "end_turn", "final_response_seq": 2}),
            ("run.state_changed", {"from": "working", "to": "completed"}),
        ]
        for sequence, (kind, payload) in enumerate(payloads, 1):
            session.add(db.RunEventRow(run_id=run_id, seq=sequence, type=kind, payload=payload))
    return attempt_id


async def project(session: AsyncSession) -> dict[str, object]:
    attempts = list(await session.scalars(select(db.WorkbenchAttempt).order_by(db.WorkbenchAttempt.attempt_id)))
    scopes = list(await session.scalars(select(db.WorkbenchScope).order_by(db.WorkbenchScope.scope_id)))
    runs = list(await session.scalars(select(db.Run).order_by(db.Run.run_id)))
    events = list(await session.scalars(select(db.RunEventRow).order_by(db.RunEventRow.run_id, db.RunEventRow.seq)))
    return {
        "attempts": [workbench.view(row) for row in attempts],
        "scopes": [{"scope_id": row.scope_id, "active_attempt": row.active_attempt, "revision": row.revision} for row in scopes],
        "runs": [
            {
                "run_id": row.run_id,
                "subject": row.subject,
                "tenant": row.tenant_id,
                "state": row.state,
                "last_seq": row.last_seq,
                "durable": row.durable,
            }
            for row in runs
        ],
        "events": [{"run_id": row.run_id, "seq": row.seq, "type": row.type, "payload": row.payload} for row in events],
    }


async def exercise(root: Path) -> dict[str, object]:
    await asyncio.to_thread(validate_owned_root, root)
    source, snapshot, restored = (root / name for name in ["source.sqlite", "snapshot.sqlite", "restored.sqlite"])
    engine = create_async_engine(URL.create("sqlite+aiosqlite", database=str(source)))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    principals = {status: fixture_principal(status.lower()) for status in ["PREPARED", "COMMITTED", "REJECTED_NO_EFFECT"]}
    attempt_ids: dict[str, str] = {}
    reader = None
    try:
        async with engine.begin() as connection:
            await connection.execute(text("PRAGMA journal_mode=WAL"))
            await connection.execute(text("PRAGMA wal_autocheckpoint=0"))
            await connection.run_sync(db.Base.metadata.create_all)
        async with sessions() as session:
            for status, principal in principals.items():
                attempt_ids[status] = await seed(session, principal, status)
            await session.commit()
            before = await project(session)
        wal = Path(str(source) + "-wal")
        if not await asyncio.to_thread(wal_present, wal):
            raise AssertionError("WAL-backed source was not exercised")
        expected = await asyncio.to_thread(backup_snapshot, source, snapshot)
        async with sessions() as session:
            later = await seed(session, fixture_principal("later-write"), "PREPARED")
            await session.commit()
            live = await project(session)
        if live == before:
            raise AssertionError("Later source write missing")
        await asyncio.to_thread(restore_snapshot, snapshot, expected, restored)
        restored_bytes = await asyncio.to_thread(file_digest, restored)
        # URI mode=ro makes writes impossible during recovery verification.
        reader = create_async_engine(URL.create("sqlite+aiosqlite", database="file:" + str(restored), query={"mode": "ro", "uri": "true"}))
        restored_sessions = async_sessionmaker(reader, expire_on_commit=False)
        checked = []
        async with restored_sessions() as session:
            if await project(session) != before:
                raise AssertionError("Restored authoritative projection differs from checkpoint")
            for status, principal in principals.items():
                scope_id = workbench.user_scope(principal)
                row = await workbench.owned(session, attempt_ids[status], scope_id)
                scope = await session.get(db.WorkbenchScope, scope_id)
                if row.status != status or row.acknowledged or scope is None or scope.active_attempt != row.attempt_id:
                    raise AssertionError("Restore changed unresolved subject lock")
                try:
                    await workbench.owned(session, row.attempt_id, workbench.user_scope(fixture_principal("foreign")))
                except HTTPException as error:
                    if error.status_code != 404:
                        raise
                else:
                    raise AssertionError("Foreign scope could read recovered attempt")
                if status == "COMMITTED":
                    run = await isolated_a2a.committed_evidence(session, row, principal)
                    events = await EventLog(session).list(run.run_id)
                    artifact = await isolated_a2a.final_artifact(session, run)
                    if len(events) != 4 or artifact is None or artifact["parts"] != [{"text": "public synthetic restored result"}]:
                        raise AssertionError("Final result or event binding changed")
                elif await session.get(db.Run, row.run_id) is not None:
                    raise AssertionError("Restore manufactured a Run")
                checked.append(status)
            if await session.get(db.WorkbenchAttempt, later) is not None:
                raise AssertionError("Snapshot unexpectedly included a later source write")
        if await asyncio.to_thread(file_digest, restored) != restored_bytes:
            raise AssertionError("Read-only verification modified restored bytes")
        async with sessions() as session:
            if await project(session) != live:
                raise AssertionError("Restore modified its owned live source")
        bad_target = root / "bad-digest.sqlite"
        try:
            await asyncio.to_thread(restore_snapshot, snapshot, "0" * 64, bad_target)
        except ValueError:
            if await asyncio.to_thread(bad_target.exists):
                raise AssertionError("Invalid snapshot created a database") from None
        else:
            raise AssertionError("Wrong digest accepted")
        return {
            "status": "PASS",
            "scope": "disposable core Runtime SQLite only",
            "wal_snapshot": True,
            "checked_states": checked,
            "source_later_write_excluded": True,
            "restored_snapshot_is_latest_authority": False,
            "unresolved_locks_preserved": True,
            "no_automatic_execution_or_ACK": True,
            "foreign_scope_denied": True,
            "read_only_restore_verification": True,
            "wrong_digest_rejected_before_creation": True,
            "authentication_scope": "in-process trusted fixture principals; not an IdP/JWT verification",
            "not_proven": [
                "MySQL/Redis recovery",
                "cross-service transactions",
                "rollback-resistant authority",
                "production backup/restore",
            ],
        }
    finally:
        if reader is not None:
            await reader.dispose()
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="aip-local-restore-") as directory:
        result = asyncio.run(exercise(Path(directory)))
    result["temporary_root_removed"] = not Path(directory).exists()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
