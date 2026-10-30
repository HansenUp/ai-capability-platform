"""Disposable F7 control-plane ledger. No real node transport or automatic execution."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException


def reject(message="node control conflict; read evidence without resending"):
    raise HTTPException(409, message)


class Ledger:
    def __init__(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="f7-node-control-")
        self.path = Path(self.temporary.name) / "authority.sqlite"
        self.path.touch(exist_ok=False)
        with self.transaction() as db:
            db.executescript("""
              CREATE TABLE nodes(id TEXT PRIMARY KEY, body TEXT NOT NULL);
              CREATE TABLE operations(id TEXT PRIMARY KEY, node TEXT NOT NULL, actor TEXT NOT NULL, body TEXT NOT NULL);
              CREATE TABLE locks(node TEXT PRIMARY KEY, operation TEXT UNIQUE NOT NULL);
              CREATE TABLE boot_challenges(node TEXT PRIMARY KEY, body TEXT NOT NULL);
            """)

    @contextmanager
    def transaction(self, *, write=True):
        db = sqlite3.connect(f"file:{self.path}?mode=rw", uri=True, timeout=0)
        try:
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        except sqlite3.OperationalError as exc:
            db.rollback()
            raise HTTPException(409, "ledger unavailable or busy; no automatic resend") from exc
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def close(self):
        self.temporary.cleanup()

    @staticmethod
    def read(db, table, identifier):
        if table not in {"nodes", "operations"}:
            raise ValueError("invalid table")
        row = db.execute(
            {"nodes": "SELECT body FROM nodes WHERE id=?", "operations": "SELECT body FROM operations WHERE id=?"}[table], (identifier,)
        ).fetchone()
        if row is None:
            raise HTTPException(404, "no evidence in scope; absence is not unlock evidence")
        return json.loads(row[0])

    @staticmethod
    def save(db, table, row):
        db.execute(
            {"nodes": "UPDATE nodes SET body=? WHERE id=?", "operations": "UPDATE operations SET body=? WHERE id=?"}[table],
            (json.dumps(row), row["id"]),
        )

    def seed(self, node, actors):
        # Fixture-only enrollment, never an HTTP operation or production migration.
        with self.transaction() as db:
            body = dict(
                id=node,
                generation=0,
                state="SERVING",
                desired="OPEN",
                owner=None,
                actors=list(actors),
                boot_id="synthetic-boot",
                synthetic=True,
            )
            db.execute("INSERT INTO nodes VALUES (?,?)", (node, json.dumps(body)))

    def authorize(self, db, node, actor):
        row = self.read(db, "nodes", node)
        if actor not in row["actors"]:
            raise HTTPException(404, "node not in authorized scope")
        return row

    def owned(self, db, attempt, actor):
        row = self.read(db, "operations", attempt)
        if row["actor"] != actor:
            raise HTTPException(404, "operation not in authorized scope")
        self.authorize(db, row["node"], actor)
        return row

    def recover(self, actor):
        with self.transaction(write=False) as db:
            rows = db.execute("SELECT body FROM operations WHERE actor=? ORDER BY rowid", (actor,)).fetchall()
            result = []
            for row in rows:
                op = json.loads(row[0])
                self.authorize(db, op["node"], actor)
                if not op["acknowledged"]:
                    result.append(op)
            return result

    def prepare(self, actor, node, action, expected, attempt, limit=None):
        if action not in {"drain", "resume", "configure_limit"} or type(expected) is not int or expected < 0:
            raise HTTPException(422, "invalid control request")
        if (action == "configure_limit" and (type(limit) is not int or limit <= 0)) or (action != "configure_limit" and limit is not None):
            raise HTTPException(422, "invalid concurrency configuration")
        with self.transaction() as db:
            state = self.authorize(db, node, actor)
            existing = db.execute("SELECT body FROM operations WHERE id=?", (attempt,)).fetchone()
            if existing:
                row = json.loads(existing[0])
                if (row["actor"], row["node"], row["action"], row["expected"], row.get("limit")) != (actor, node, action, expected, limit):
                    reject()
                return row
            if db.execute("SELECT 1 FROM locks WHERE node=?", (node,)).fetchone():
                reject("node has unresolved control operation")
            allowed = {"SERVING", "DRAINED"} if action == "configure_limit" else {"SERVING" if action == "drain" else "DRAINED"}
            if state["generation"] != expected or state["state"] not in allowed:
                reject()
            if action == "resume" and state["owner"] != actor:
                raise HTTPException(403, "original drain operator required")
            row = dict(
                id=attempt,
                node=node,
                actor=actor,
                action=action,
                expected=expected,
                generation=expected + 1,
                status="PREPARED",
                acknowledged=False,
                evidence=None,
                version=1,
            )
            if action == "configure_limit":
                row["limit"] = limit
            db.execute("INSERT INTO operations VALUES (?,?,?,?)", (attempt, node, actor, json.dumps(row)))
            db.execute("INSERT INTO locks VALUES (?,?)", (node, attempt))
            return row

    def execute(self, actor, attempt):
        return self.claim(actor, attempt)[0]

    def claim(self, actor, attempt):
        """Return durable operation plus whether this caller alone claimed execution."""
        with self.transaction() as db:
            op = self.owned(db, attempt, actor)
            if op["status"] != "PREPARED":
                return op, False  # Read existing result, never dispatch again.
            state = self.authorize(db, op["node"], actor)
            if state["generation"] != op["expected"]:
                reject()
            state["generation"] = op["generation"]
            if op["action"] != "configure_limit":
                state.update(
                    desired="CLOSED" if op["action"] == "drain" else "OPEN",
                    state="DRAINING" if op["action"] == "drain" else "RESUMING",
                )
            if op["action"] == "drain":
                state["owner"] = actor
            op["status"] = "EXECUTING"
            self.save(db, "nodes", state)
            self.save(db, "operations", op)
            return op, True

    def withdraw(self, actor, attempt):
        with self.transaction() as db:
            op = self.owned(db, attempt, actor)
            if op["status"] == "CANCELLED":
                return op
            if op["status"] != "PREPARED":
                reject("only unexecuted preparation may be withdrawn")
            op.update(status="CANCELLED", evidence=dict(receipt_id=str(uuid4()), version=1, outcome="cancelled"))
            self.save(db, "operations", op)
            return op

    def reconcile(self, attempt, evidence):
        # Called only by a trusted fixture adapter after reading durable node evidence.
        # No node invocation, no caller supplied HTTP trusted flag.
        with self.transaction() as db:
            op = self.read(db, "operations", attempt)
            state = self.read(db, "nodes", op["node"])
            expected = {
                "attempt": attempt,
                "node": op["node"],
                "action": op["action"],
                "generation": op["generation"],
                "boot_id": state["boot_id"],
                "actor": op["actor"],
                "version": 1,
            }
            if op["action"] == "configure_limit":
                expected["limit"] = op["limit"]
            if any(evidence.get(key) != value or type(evidence.get(key)) is not type(value) for key, value in expected.items()):
                reject("node evidence binding mismatch")
            if op["status"] != "EXECUTING":
                if op["evidence"] == evidence:
                    return op
                reject("terminal evidence conflict")
            if state["generation"] != op["generation"]:
                reject("stale generation")
            outcome = evidence.get("outcome")
            target = {"drain": "drained", "resume": "serving", "configure_limit": "limit_applied"}[op["action"]]
            if outcome not in {target, "timeout"} or (outcome == "timeout" and op["action"] != "drain"):
                reject("invalid effect evidence")
            if outcome == target:
                if target == "limit_applied":
                    state["http_concurrency_limit"] = op["limit"]
                else:
                    state["state"] = "DRAINED" if target == "drained" else "SERVING"
                op["status"] = "SUCCEEDED"
            else:
                op["status"] = "FAILED"  # Node remains closed/draining; ACK cannot reopen it.
            op["evidence"] = evidence
            self.save(db, "nodes", state)
            self.save(db, "operations", op)
            return op

    def ack(self, actor, attempt, receipt):
        with self.transaction() as db:
            op = self.owned(db, attempt, actor)
            if op["status"] not in {"SUCCEEDED", "FAILED", "CANCELLED"} or json.dumps(op, sort_keys=True) != json.dumps(
                receipt, sort_keys=True
            ):
                reject("ACK requires exact current terminal receipt")
            if op["acknowledged"]:
                return op
            op["acknowledged"] = True
            self.save(db, "operations", op)
            db.execute("DELETE FROM locks WHERE node=? AND operation=?", (op["node"], attempt))
            return op

    def observe_drained(self, attempt, evidence):
        """Late natural drain observation does not rewrite the terminal timeout receipt or ACK."""
        with self.transaction() as db:
            op = self.read(db, "operations", attempt)
            node = self.read(db, "nodes", op["node"])
            expected = dict(
                attempt=attempt,
                node=op["node"],
                actor=op["actor"],
                action="drain",
                generation=op["generation"],
                boot_id=node["boot_id"],
                version=1,
                outcome="observed_drained",
                active_requests=0,
                active_workers=0,
            )
            if (
                json.dumps(evidence, sort_keys=True) != json.dumps(expected, sort_keys=True)
                or op["action"] != "drain"
                or op["status"] != "FAILED"
                or op["evidence"]["outcome"] != "timeout"
                or node["generation"] != op["generation"]
                or node["desired"] != "CLOSED"
                or node["state"] not in {"DRAINING", "DRAINED"}
            ):
                reject("natural drain evidence mismatch")
            node["state"] = "DRAINED"
            node["observation"] = evidence
            self.save(db, "nodes", node)
            return node

    def snapshot(self, actor):
        with self.transaction(write=False) as db:
            rows = db.execute("SELECT body FROM nodes ORDER BY id").fetchall()
            items = []
            for raw in rows:
                node = json.loads(raw[0])
                if actor not in node["actors"]:
                    continue
                busy = db.execute("SELECT 1 FROM locks WHERE node=?", (node["id"],)).fetchone() is not None
                recovery = None
                if node["state"] == "DRAINING" and node["owner"] == actor:
                    latest = db.execute(
                        "SELECT body FROM operations WHERE node=? AND actor=? ORDER BY rowid DESC LIMIT 1", (node["id"], actor)
                    ).fetchone()
                    if latest:
                        op = json.loads(latest[0])
                        if op["generation"] == node["generation"] and op["status"] == "FAILED" and op["action"] == "drain":
                            recovery = op["id"]
                items.append(
                    {key: node[key] for key in ("id", "generation", "state", "desired", "boot_id", "synthetic")}
                    | {
                        "http_concurrency_limit": node.get("http_concurrency_limit"),
                        "control_busy": busy,
                        "recovery_attempt": recovery,
                        "can_resume": node["owner"] == actor and node["state"] == "DRAINED" and not busy,
                    }
                )
            return items

    def boot_challenge(self, node_id):
        """Fixture service adapter only. Metadata challenge cannot change admission or unlock."""
        with self.transaction() as db:
            node = self.read(db, "nodes", node_id)
            if db.execute("SELECT 1 FROM locks WHERE node=?", (node_id,)).fetchone() or node["state"] not in {"SERVING", "DRAINED"}:
                reject("unresolved node control must be reconciled before boot handoff")
            proof = dict(
                node=node_id,
                previous_boot=node["boot_id"],
                generation=node["generation"],
                desired=node["desired"],
                version=1,
                nonce=str(uuid4()),
                expires_at=int(time.time()) + 60,
            )
            db.execute("INSERT OR REPLACE INTO boot_challenges VALUES (?,?)", (node_id, json.dumps(proof)))
            return proof

    def accept_boot(self, node_id, proof):
        with self.transaction() as db:
            row = db.execute("SELECT body FROM boot_challenges WHERE node=?", (node_id,)).fetchone()
            if row is None:
                reject("no outstanding boot challenge; read current binding")
            challenge = json.loads(row[0])
            node = self.read(db, "nodes", node_id)
            new_boot = proof.get("new_boot")
            expected = challenge | {"new_boot": new_boot}
            if (
                json.dumps(proof, sort_keys=True) != json.dumps(expected, sort_keys=True)
                or not isinstance(new_boot, str)
                or not 1 <= len(new_boot) <= 64
                or new_boot == node["boot_id"]
                or challenge["expires_at"] <= int(time.time())
                or node["boot_id"] != challenge["previous_boot"]
                or node["generation"] != challenge["generation"]
                or node["desired"] != challenge["desired"]
                or node["state"] not in {"SERVING", "DRAINED"}
                or db.execute("SELECT 1 FROM locks WHERE node=?", (node_id,)).fetchone()
            ):
                reject("boot evidence stale, conflicting or node busy")
            node["boot_id"] = new_boot
            self.save(db, "nodes", node)
            db.execute("DELETE FROM boot_challenges WHERE node=?", (node_id,))
            return node
