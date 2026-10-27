"""Local experimental version ledger; not a production trust anchor."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from threading import RLock

from .local_policy import LocalPolicyRule, LocalReadDataPolicy
from .query import Identity, Query, QueryDenied
from .reviewed import AccessDecision, AccessRequest, ReviewedBinding

_LOCAL_LOCK = RLock()


class VersionDenied(Exception):
    """Fail closed without leaking storage details."""


@dataclass(frozen=True)
class Record:
    kind: str
    tenant: str
    view: str
    revision: int
    state: str
    payload: str
    command: str
    previous: str
    manifest_revision: int
    manifest_hash: str
    digest: str


def _digest(fields: tuple[object, ...]) -> str:
    return hashlib.sha256(json.dumps(fields, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()


class VersionLedger:
    """Local in-process serialization; other processes can still fail fast on SQLite locks.

    Privileged file rollback or rewrite remains undetectable.
    """

    def __init__(self, path: Path):
        self.path = path

    @classmethod
    def create(cls, path: Path) -> VersionLedger:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        try:
            with closing(sqlite3.connect(path, timeout=0)) as db:
                db.executescript("""
                CREATE TABLE events (kind TEXT NOT NULL, tenant TEXT NOT NULL, view_id TEXT NOT NULL,
                    revision INTEGER NOT NULL, state TEXT NOT NULL, payload TEXT NOT NULL,
                    command TEXT NOT NULL, previous TEXT NOT NULL, manifest_revision INTEGER NOT NULL,
                    manifest_hash TEXT NOT NULL, digest TEXT NOT NULL,
                    PRIMARY KEY(kind, tenant, view_id, revision), UNIQUE(kind, tenant, view_id, command));
                CREATE TABLE heads (kind TEXT NOT NULL, tenant TEXT NOT NULL, view_id TEXT NOT NULL,
                    revision INTEGER NOT NULL, digest TEXT NOT NULL, PRIMARY KEY(kind, tenant, view_id));
                CREATE TRIGGER events_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT, 'immutable'); END;
                CREATE TRIGGER events_no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT, 'immutable'); END;
                """)
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return cls(path)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path.resolve().as_uri() + "?mode=rw", uri=True, timeout=0, isolation_level=None)

    @staticmethod
    def _read(db: sqlite3.Connection, kind: str, tenant: str, view: str) -> Record | None:
        rows = db.execute(
            "SELECT kind,tenant,view_id,revision,state,payload,command,previous,manifest_revision,manifest_hash,digest "
            "FROM events WHERE kind=? AND tenant=? AND view_id=? ORDER BY revision",
            (kind, tenant, view),
        ).fetchall()
        head = db.execute("SELECT revision,digest FROM heads WHERE kind=? AND tenant=? AND view_id=?", (kind, tenant, view)).fetchone()
        previous = ""
        for index, row in enumerate(rows, 1):
            if row[3] != index or row[7] != previous or _digest(row[:-1]) != row[-1] or row[4] not in ("active", "revoked"):
                raise VersionDenied()
            previous = row[-1]
        if not rows:
            if head is not None:
                raise VersionDenied()
            return None
        if head != (len(rows), previous):
            raise VersionDenied()
        return Record(*rows[-1])

    def append(
        self,
        kind: str,
        tenant: str,
        view: str,
        *,
        expected: int,
        state: str,
        payload: str,
        command: str,
        manifest: tuple[int, str] | None = None,
    ) -> Record:
        try:
            if (
                kind not in ("manifest", "policy")
                or any(not isinstance(x, str) or not x.strip() or len(x) > 256 for x in (tenant, view))
                or type(expected) is not int
                or expected < 0
                or state not in ("active", "revoked")
                or not isinstance(payload, str)
                or len(payload.encode()) > 65536
                or (state == "active") != bool(payload)
                or str(uuid.UUID(command)) != command
            ):
                raise VersionDenied()
            if kind == "manifest" and manifest is not None:
                raise VersionDenied()
            with _LOCAL_LOCK, closing(self._connect()) as db:
                db.execute("BEGIN IMMEDIATE")
                old = self._read(db, kind, tenant, view)
                if (old.revision if old else 0) != expected or (state == "revoked" and (old is None or old.state != "active")):
                    raise VersionDenied()
                manifest_revision, manifest_hash = 0, ""
                if kind == "policy" and state == "active":
                    current = self._read(db, "manifest", tenant, view)
                    if current is None or current.state != "active" or manifest != (current.revision, current.digest):
                        raise VersionDenied()
                    manifest_revision, manifest_hash = manifest
                elif manifest is not None:
                    raise VersionDenied()
                fields = (
                    kind,
                    tenant,
                    view,
                    expected + 1,
                    state,
                    payload,
                    command,
                    old.digest if old else "",
                    manifest_revision,
                    manifest_hash,
                )
                digest = _digest(fields)
                db.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?)", (*fields, digest))
                db.execute(
                    "INSERT INTO heads VALUES (?,?,?,?,?) ON CONFLICT(kind,tenant,view_id) "
                    "DO UPDATE SET revision=excluded.revision,digest=excluded.digest",
                    (kind, tenant, view, expected + 1, digest),
                )
                db.commit()
                return Record(*fields, digest)
        except Exception:
            raise VersionDenied() from None

    def pair(self, tenant: str, view: str) -> tuple[Record, Record]:
        try:
            with _LOCAL_LOCK, closing(self._connect()) as db:
                db.execute("BEGIN")
                manifest = self._read(db, "manifest", tenant, view)
                policy = self._read(db, "policy", tenant, view)
                if (
                    manifest is None
                    or policy is None
                    or manifest.state != "active"
                    or policy.state != "active"
                    or (policy.manifest_revision, policy.manifest_hash) != (manifest.revision, manifest.digest)
                ):
                    raise VersionDenied()
                return manifest, policy
        except Exception:
            raise VersionDenied() from None


class VersionedProviders:
    """Injected decoders are trusted test fixtures, not authenticated publishers."""

    def __init__(
        self,
        ledger: VersionLedger,
        binding_decoder: Callable[[Record], ReviewedBinding],
        policy_decoder: Callable[[Record, Record], LocalPolicyRule],
    ) -> None:
        self.ledger = ledger
        self.binding_decoder = binding_decoder
        self.policy_decoder = policy_decoder

    async def binding(self, identity: Identity, query: Query) -> ReviewedBinding:
        try:
            manifest, _ = self.ledger.pair(identity.tenant, query.view_id)
            binding = self.binding_decoder(manifest)
            if (binding.tenant, binding.view_id, binding.generation) != (identity.tenant, query.view_id, manifest.revision):
                raise QueryDenied()
            return binding
        except Exception:
            raise QueryDenied() from None

    async def policy(self, request: AccessRequest) -> AccessDecision:
        try:
            manifest, policy = self.ledger.pair(request.user.tenant, request.view_id)
            if manifest.revision != request.generation:
                raise QueryDenied()
            rule = self.policy_decoder(policy, manifest)
            return await LocalReadDataPolicy(f"{policy.revision}:{policy.digest}", (rule,))(request)
        except Exception:
            raise QueryDenied() from None
