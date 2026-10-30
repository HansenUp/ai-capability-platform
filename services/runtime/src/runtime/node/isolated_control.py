"""Injected synthetic node application journal for F7, never used by production startup."""

import json
import sqlite3
import tempfile
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4


class SyntheticNode:
    def __init__(self, node_id):
        self.temporary = tempfile.TemporaryDirectory(prefix="f7-synthetic-node-")
        self.path = Path(self.temporary.name) / "applied.sqlite"
        self.path.touch(exist_ok=False)
        self.node_id = node_id
        self.boot_id = "synthetic-boot"
        self.accepting = False
        self.active = 0
        self.applied_calls = 0
        with self.connection() as db:
            db.executescript(
                "CREATE TABLE state(id INTEGER PRIMARY KEY, generation INTEGER, desired TEXT);"
                "INSERT INTO state VALUES(1,0,'CLOSED');"
                "CREATE TABLE configuration(id INTEGER PRIMARY KEY, http_limit INTEGER);"
                "INSERT INTO configuration VALUES(1,NULL);"
                "CREATE TABLE effects(attempt TEXT PRIMARY KEY, body TEXT NOT NULL);"
                "CREATE TABLE boots(id INTEGER PRIMARY KEY, current TEXT, previous TEXT);"
                "INSERT INTO boots VALUES(1,'synthetic-boot',NULL);"
            )

    @contextmanager
    def connection(self):
        try:
            db = sqlite3.connect(f"file:{self.path}?mode=rw", uri=True, timeout=0)
        except sqlite3.Error:
            self.accepting = False
            raise
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            self.accepting = False
            raise
        finally:
            db.close()

    def startup(self, authority):
        self.accepting = False
        with self.connection() as db:
            generation, desired = db.execute("SELECT generation,desired FROM state WHERE id=1").fetchone()
            if authority["id"] != self.node_id or authority["generation"] != generation or authority["boot_id"] != self.boot_id:
                raise ValueError("unreconciled generation")
            if authority["desired"] != desired or authority["state"] not in {"DRAINED", "SERVING"}:
                raise ValueError("unreconciled admission state")
            limit = db.execute("SELECT http_limit FROM configuration WHERE id=1").fetchone()[0]
            if authority.get("http_concurrency_limit") != limit:
                raise ValueError("unreconciled concurrency configuration")
            self.accepting = desired == "OPEN" and authority["state"] == "SERVING"

    def apply(self, operation, authority):
        if operation.get("action") == "configure_limit":
            return self.apply_limit(operation, authority)
        if operation.get("action") not in {"drain", "resume"}:
            raise ValueError("unsupported node action")
        # Called only after explicit CAS claim and current user permission check by fixture adapter.
        if (
            operation["node"] != self.node_id
            or operation["status"] != "EXECUTING"
            or authority["id"] != self.node_id
            or authority["generation"] != operation["generation"]
            or authority["boot_id"] != self.boot_id
        ):
            self.accepting = False
            raise ValueError("invalid delegation scope")
        with self.connection() as db:
            existing = db.execute("SELECT body FROM effects WHERE attempt=?", (operation["id"],)).fetchone()
            if existing:
                raise ValueError("effect already applied; read evidence")
            generation, desired = db.execute("SELECT generation,desired FROM state WHERE id=1").fetchone()
            if generation != operation["expected"]:
                raise ValueError("stale generation")
            drain = operation["action"] == "drain"
            if not drain and (desired != "CLOSED" or self.active):
                raise ValueError("not drained")
            desired = "CLOSED" if drain else "OPEN"
            if authority["desired"] != desired:
                raise ValueError("desired state mismatch")
            # This synthetic boundary models atomic local application+journal. Real lifecycle adapter remains separate.
            self.accepting = not drain
            outcome = ("timeout" if self.active else "drained") if drain else "serving"
            evidence = dict(
                attempt=operation["id"],
                node=self.node_id,
                action=operation["action"],
                generation=operation["generation"],
                boot_id=self.boot_id,
                actor=operation["actor"],
                version=1,
                outcome=outcome,
            )
            db.execute("UPDATE state SET generation=?,desired=? WHERE id=1", (operation["generation"], desired))
            db.execute("INSERT INTO effects VALUES (?,?)", (operation["id"], json.dumps(evidence)))
        self.applied_calls += 1
        return evidence

    def apply_limit(self, operation, authority):
        """Persist configuration and its bound receipt without opening admission."""
        limit = operation.get("limit")
        if (
            operation.get("action") != "configure_limit"
            or type(limit) is not int
            or limit <= 0
            or operation["node"] != self.node_id
            or operation["status"] != "EXECUTING"
            or authority["id"] != self.node_id
            or authority["generation"] != operation["generation"]
            or authority["boot_id"] != self.boot_id
        ):
            raise ValueError("invalid concurrency delegation")
        with self.connection() as db:
            generation = db.execute("SELECT generation FROM state WHERE id=1").fetchone()[0]
            if generation != operation["expected"] or db.execute("SELECT 1 FROM effects WHERE attempt=?", (operation["id"],)).fetchone():
                raise ValueError("stale or already applied configuration; read evidence")
            evidence = dict(
                attempt=operation["id"],
                node=self.node_id,
                action="configure_limit",
                generation=operation["generation"],
                boot_id=self.boot_id,
                actor=operation["actor"],
                version=1,
                outcome="limit_applied",
                limit=limit,
            )
            db.execute("UPDATE configuration SET http_limit=? WHERE id=1", (limit,))
            db.execute("UPDATE state SET generation=? WHERE id=1", (operation["generation"],))
            db.execute("INSERT INTO effects VALUES (?,?)", (operation["id"], json.dumps(evidence)))
        self.applied_calls += 1
        return evidence

    def read(self, attempt):
        # Pure journal read: never reapplies an unknown operation.
        db = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=0)
        try:
            row = db.execute("SELECT body FROM effects WHERE attempt=?", (attempt,)).fetchone()
            return json.loads(row[0]) if row else None
        finally:
            db.close()

    def observe_drained(self, attempt):
        original = self.read(attempt)
        if not original or original["action"] != "drain" or original["outcome"] != "timeout" or self.active or self.accepting:
            raise ValueError("no completed natural drain observation")
        with self.connection() as db:
            generation, desired = db.execute("SELECT generation,desired FROM state WHERE id=1").fetchone()
            if generation != original["generation"] or desired != "CLOSED":
                raise ValueError("stale drain observation")
        return original | {"boot_id": self.boot_id, "outcome": "observed_drained", "active_requests": 0, "active_workers": 0}

    def reboot(self):
        self.accepting = False
        with self.connection() as db:
            old = db.execute("SELECT current FROM boots WHERE id=1").fetchone()[0]
            self.boot_id = uuid4().hex
            db.execute("UPDATE boots SET current=?,previous=? WHERE id=1", (self.boot_id, old))

    def boot_proof(self, challenge):
        self.accepting = False
        with self.connection() as db:
            current, previous = db.execute("SELECT current,previous FROM boots WHERE id=1").fetchone()
            generation, desired = db.execute("SELECT generation,desired FROM state WHERE id=1").fetchone()
            if (
                self.active
                or challenge["node"] != self.node_id
                or challenge["previous_boot"] != previous
                or challenge["generation"] != generation
                or challenge["desired"] != desired
                or self.boot_id != current
            ):
                raise ValueError("boot challenge does not match durable node journal")
            return challenge | {"new_boot": current}

    def close(self):
        self.temporary.cleanup()


class LifecycleNode(SyntheticNode):
    """Temporary journal adapter exercising the existing admission middleware with fake workers.

    Not installed in production startup. A cancelled/unknown application stays closed;
    recovery reads the journal and never invokes this method a second time.
    """

    def __init__(self, node_id, lifecycle):
        super().__init__(node_id)
        self.lifecycle = lifecycle
        self.lifecycle.close_admission()

    async def apply_lifecycle(self, operation, authority, timeout_s):
        import asyncio
        import math

        if type(timeout_s) not in (int, float) or not math.isfinite(timeout_s) or not 0 < timeout_s <= 300:
            raise ValueError("invalid bounded drain deadline")
        if operation.get("action") == "configure_limit":
            try:
                result = self.apply_limit(operation, authority)
                # Synchronous commit + update has no event-loop yield; recovery/startup uses durable config.
                self.lifecycle.set_http_concurrency_limit(result["limit"])
                return result
            except BaseException:
                self.lifecycle.close_admission()
                raise
        self.lifecycle.close_admission()
        timed_out = False
        try:
            if operation["action"] == "drain":
                try:
                    async with asyncio.timeout(timeout_s):
                        await self.lifecycle.wait_idle()
                except TimeoutError:
                    timed_out = True
            elif (
                operation["action"] != "resume"
                or not self.lifecycle.http_idle.is_set()
                or (self.lifecycle.background.worker is not None and not self.lifecycle.background.worker.idle.is_set())
            ):
                raise ValueError("resume requires idle admission and worker")
            self.active = 1 if timed_out else 0
            result = super().apply(operation, authority)
            if operation["action"] == "resume":
                # Open only after the local journal transaction committed.
                self.lifecycle.resume()
            return result
        except BaseException:
            self.accepting = False
            self.lifecycle.close_admission()
            raise

    def observe_drained(self, attempt):
        self.active = int(
            not self.lifecycle.http_idle.is_set()
            or (self.lifecycle.background.worker is not None and not self.lifecycle.background.worker.idle.is_set())
        )
        return super().observe_drained(attempt)

    def startup(self, authority):
        self.lifecycle.close_admission()
        super().startup(authority)
        with self.connection() as db:
            limit = db.execute("SELECT http_limit FROM configuration WHERE id=1").fetchone()[0]
        if limit is not None:
            self.lifecycle.set_http_concurrency_limit(limit)
        if self.accepting:
            self.lifecycle.resume()
