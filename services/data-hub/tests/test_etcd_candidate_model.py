"""etcd-style subset modeled in temporary SQLite; does not implement or contact etcd."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass
from threading import Barrier, Lock

import pytest


class Unavailable(Exception):
    pass


class Unknown(Exception):
    pass


@dataclass(frozen=True)
class Item:
    value: str
    mod_revision: int


class FakeEtcd:
    """A narrow transaction fake; locking, revision and read behavior are assumptions."""

    def __init__(self, path):
        self.path = path
        self.lock = Lock()
        self.available = True
        with closing(sqlite3.connect(path)) as db:
            db.execute("CREATE TABLE kv(key TEXT PRIMARY KEY, value TEXT NOT NULL, mod_revision INTEGER NOT NULL)")
            db.execute("CREATE TABLE revision(id INTEGER PRIMARY KEY CHECK(id=1), value INTEGER NOT NULL)")
            db.execute("INSERT INTO revision VALUES(1,0)")
            db.commit()

    def read(self, keys, *, serializable=False):
        if serializable:
            raise ValueError("stale reads forbidden by model")
        with self.lock, closing(sqlite3.connect(self.path)) as db:
            if not self.available:
                raise Unavailable()
            db.execute("BEGIN")
            revision = db.execute("SELECT value FROM revision WHERE id=1").fetchone()[0]
            values = {}
            for key in keys:
                row = db.execute("SELECT value,mod_revision FROM kv WHERE key=?", (key,)).fetchone()
                values[key] = None if row is None else Item(*row)
            return revision, values

    def txn(self, compares, puts, *, fail=None):
        with self.lock, closing(sqlite3.connect(self.path, isolation_level=None, timeout=0)) as db:
            if not self.available:
                raise Unavailable()
            db.execute("BEGIN IMMEDIATE")
            revision = db.execute("SELECT value FROM revision WHERE id=1").fetchone()[0]
            for key, expected_mod_revision in compares.items():
                row = db.execute("SELECT mod_revision FROM kv WHERE key=?", (key,)).fetchone()
                observed = row[0] if row else 0
                if observed != expected_mod_revision:
                    db.rollback()
                    return False, revision
            if fail == "before_commit":
                raise Unknown()
            revision += 1
            for key, value in puts.items():
                db.execute(
                    "INSERT INTO kv VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,mod_revision=excluded.mod_revision",
                    (key, value, revision),
                )
            db.execute("UPDATE revision SET value=? WHERE id=1", (revision,))
            db.commit()
            if fail == "after_commit":
                raise Unknown()
            return True, revision


class Denied(Exception):
    pass


class FakeWitness:
    def __init__(self):
        self.values = {}
        self.available = True

    def read(self, scope):
        if not self.available:
            raise Unavailable()
        return self.values.get(scope, (0, "genesis"))


class Candidate:
    def __init__(self, kv, witness):
        self.kv, self.witness = kv, witness

    def publish(self, scope, command, expected_version, digest, *, fault=None):
        key, receipt_key = f"head/{scope}", f"receipt/{scope}/{command}"
        _, observed = self.kv.read((key, receipt_key))
        current = observed[key]
        expected_revision = current.mod_revision if current else 0
        current_version = json.loads(current.value)["version"] if current else 0
        if current_version != expected_version or observed[receipt_key] is not None:
            raise Denied()
        # The witness is checked but not updated automatically.
        current_digest = json.loads(current.value)["digest"] if current else "genesis"
        if self.witness.read(scope) != (expected_version, current_digest):
            raise Denied()
        new = json.dumps({"scope": scope, "version": expected_version + 1, "digest": digest}, sort_keys=True)
        success, revision = self.kv.txn({key: expected_revision, receipt_key: 0}, {key: new, receipt_key: new}, fail=fault)
        if not success:
            raise Denied()
        return revision

    def read_allowed(self, scope):
        _, values = self.kv.read((f"head/{scope}",))
        item = values[f"head/{scope}"]
        if item is None:
            raise Denied()
        head = json.loads(item.value)
        if self.witness.read(scope) != (head["version"], head["digest"]):
            raise Denied()
        return head


@pytest.fixture
def model(tmp_path):
    return Candidate(FakeEtcd(tmp_path / "etcd-substitute.db"), FakeWitness())


def test_atomic_head_and_receipt_share_commit_revision_and_pending_denies(model):
    revision = model.publish("tenant-a/view", "cmd-1", 0, "digest-1")
    observed_revision, values = model.kv.read(("head/tenant-a/view", "receipt/tenant-a/view/cmd-1"))
    assert observed_revision == revision == 1
    assert values["head/tenant-a/view"].mod_revision == values["receipt/tenant-a/view/cmd-1"].mod_revision == revision
    with pytest.raises(Denied):
        model.read_allowed("tenant-a/view")
    with pytest.raises(Denied):
        model.publish("tenant-a/view", "cmd-2", 1, "digest-2")
    model.witness.values["tenant-a/view"] = (1, "digest-1")  # explicit isolated witness confirmation
    assert model.read_allowed("tenant-a/view")["digest"] == "digest-1"


def test_stale_cas_and_duplicate_command_have_no_partial_writes(model):
    model.publish("tenant-a/view", "cmd-1", 0, "digest-1")
    model.witness.values["tenant-a/view"] = (1, "digest-1")
    with pytest.raises(Denied):
        model.publish("tenant-a/view", "cmd-1", 1, "different")
    with pytest.raises(Denied):
        model.publish("tenant-a/view", "cmd-2", 0, "stale")
    revision, items = model.kv.read(("head/tenant-a/view", "receipt/tenant-a/view/cmd-2"))
    assert revision == 1 and items["receipt/tenant-a/view/cmd-2"] is None


def test_failure_before_commit_and_missing_receipt_do_not_authorize_retry(model):
    with pytest.raises(Unknown):
        model.publish("tenant-a/view", "cmd-1", 0, "digest-1", fault="before_commit")
    revision, items = model.kv.read(("head/tenant-a/view", "receipt/tenant-a/view/cmd-1"))
    assert revision == 0 and all(item is None for item in items.values())
    # An absent receipt alone is not a formal proof of a terminal non-commit.
    with pytest.raises(Denied):
        model.read_allowed("tenant-a/view")


def test_lost_commit_response_recovery_is_read_only_and_scope_bound(model):
    with pytest.raises(Unknown):
        model.publish("tenant-a/view", "cmd-1", 0, "digest-1", fault="after_commit")
    _, rows = model.kv.read(("receipt/tenant-a/view/cmd-1", "receipt/tenant-b/view/cmd-1"))
    receipt = json.loads(rows["receipt/tenant-a/view/cmd-1"].value)
    assert receipt == {"scope": "tenant-a/view", "version": 1, "digest": "digest-1"}
    assert rows["receipt/tenant-b/view/cmd-1"] is None
    with pytest.raises(Denied):
        model.read_allowed("tenant-a/view")
    model.witness.values["tenant-a/view"] = (1, "digest-1")
    assert model.read_allowed("tenant-a/view") == receipt
    with pytest.raises(Denied):
        model.publish("tenant-a/view", "cmd-1", 1, "digest-1")


def test_competing_cas_writes_at_most_one_commits(model):
    barrier = Barrier(2)

    def worker(command):
        barrier.wait()
        try:
            return model.publish("tenant-a/view", command, 0, command)
        except Denied:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, ("c1", "c2")))
    assert sum(item is not None for item in results) == 1
    revision, rows = model.kv.read(("receipt/tenant-a/view/c1", "receipt/tenant-a/view/c2"))
    assert revision == 1 and sum(value is not None for value in rows.values()) == 1


def test_unavailable_stale_read_and_witness_ahead_deny(model):
    model.publish("tenant-a/view", "cmd-1", 0, "digest-1")
    model.witness.values["tenant-a/view"] = (2, "digest-2")
    with pytest.raises(Denied):
        model.read_allowed("tenant-a/view")
    with pytest.raises(ValueError):
        model.kv.read(("head/tenant-a/view",), serializable=True)
    model.kv.available = False
    with pytest.raises(Unavailable):
        model.read_allowed("tenant-a/view")


def test_matching_version_wrong_digest_and_witness_unavailable_deny(model):
    model.publish("tenant-a/view", "cmd-1", 0, "digest-1")
    model.witness.values["tenant-a/view"] = (1, "wrong")
    with pytest.raises(Denied):
        model.read_allowed("tenant-a/view")
    with pytest.raises(Denied):
        model.publish("tenant-a/view", "cmd-2", 1, "digest-2")
    model.witness.available = False
    with pytest.raises(Unavailable):
        model.read_allowed("tenant-a/view")
