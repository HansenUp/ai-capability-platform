"""Two temporary SQLite substitutes; neither is a production authority or witness."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec


class Denied(Exception):
    pass


@dataclass(frozen=True)
class Head:
    scope: str
    epoch: str
    version: int
    digest: str
    active: bool


class FakeStore:
    def __init__(self, path: Path, signer, issuer: str):
        self.path, self.key, self.issuer = path, signer, issuer
        self.available = True
        with closing(sqlite3.connect(path)) as db:
            db.execute("CREATE TABLE heads(scope TEXT PRIMARY KEY, epoch TEXT, version INTEGER, digest TEXT, active INTEGER)")
            db.execute("CREATE TABLE commands(scope TEXT, command TEXT, version INTEGER, digest TEXT, PRIMARY KEY(scope, command))")
            db.commit()

    def head(self, scope):
        if not self.available:
            raise Denied()
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute("SELECT scope,epoch,version,digest,active FROM heads WHERE scope=?", (scope,)).fetchone()
        if row is None:
            return Head(scope, "epoch-1", 0, "genesis", False)
        return Head(row[0], row[1], row[2], row[3], bool(row[4]))

    def transition(self, old, new, command):
        if not self.available or new.scope != old.scope or new.epoch != old.epoch or new.version != old.version + 1:
            raise Denied()
        with closing(sqlite3.connect(self.path, isolation_level=None, timeout=0)) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT epoch,version,digest,active FROM heads WHERE scope=?", (old.scope,)).fetchone()
            actual = (old.epoch, old.version, old.digest, int(old.active))
            if (row is None and old.version != 0) or (row is not None and row != actual):
                raise Denied()
            if db.execute("SELECT 1 FROM commands WHERE scope=? AND command=?", (old.scope, command)).fetchone():
                raise Denied()
            db.execute(
                "INSERT INTO heads VALUES (?,?,?,?,?) ON CONFLICT(scope) DO UPDATE SET epoch=excluded.epoch, "
                "version=excluded.version,digest=excluded.digest,active=excluded.active",
                (new.scope, new.epoch, new.version, new.digest, int(new.active)),
            )
            db.execute("INSERT INTO commands VALUES (?,?,?,?)", (new.scope, command, new.version, new.digest))
            db.commit()

    def receipt(self, scope, command):
        if not self.available:
            raise Denied()
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute("SELECT scope,command,version,digest FROM commands WHERE scope=? AND command=?", (scope, command)).fetchone()

    def proof(self, scope, nonce, aud="query-test"):
        head = self.head(scope)
        payload = {
            "iss": self.issuer,
            "aud": aud,
            "nonce": nonce,
            "scope": head.scope,
            "epoch": head.epoch,
            "version": head.version,
            "digest": head.digest,
            "active": head.active,
        }
        return jwt.encode(payload, self.key, algorithm="ES256")


class Model:
    def __init__(self, authority, witness):
        self.authority, self.witness = authority, witness

    def prepare(self, scope, command, payload, active=True):
        old, seen = self.authority.head(scope), self.witness.head(scope)
        if old != seen:
            raise Denied()
        digest = hashlib.sha256(json.dumps([scope, old.version + 1, payload, active]).encode()).hexdigest()
        new = Head(scope, old.epoch, old.version + 1, digest, active)
        self.authority.transition(old, new, command)
        return old, new

    def confirm(self, old, new, command):
        current = self.authority.head(old.scope)
        receipt = self.authority.receipt(old.scope, command)
        if current != new or receipt != (old.scope, command, new.version, new.digest):
            raise Denied()
        self.witness.transition(old, new, command)

    @staticmethod
    def release_result(initial, final, result):
        if initial != final:
            raise Denied()
        return result

    def validated_read(self, scope, nonce, a_proof=None, w_proof=None):
        a_proof = a_proof or self.authority.proof(scope, nonce)
        w_proof = w_proof or self.witness.proof(scope, nonce)

        def decode(raw, source):
            try:
                claims = jwt.decode(raw, source.key.public_key(), algorithms=["ES256"], audience="query-test", issuer=source.issuer)
                if (claims.get("nonce"), claims.get("scope")) != (nonce, scope):
                    raise Denied()
                if type(claims.get("version")) is not int or type(claims.get("active")) is not bool:
                    raise Denied()
                return tuple(claims.get(x) for x in ("scope", "epoch", "version", "digest", "active"))
            except Exception:
                raise Denied() from None

        a, w = decode(a_proof, self.authority), decode(w_proof, self.witness)
        if a != w or not a[-1]:
            raise Denied()
        return a


@pytest.fixture
def model(tmp_path):
    authority = FakeStore(tmp_path / "authority.db", ec.generate_private_key(ec.SECP256R1()), "authority-test")
    witness = FakeStore(tmp_path / "witness.db", ec.generate_private_key(ec.SECP256R1()), "witness-test")
    return Model(authority, witness)


def nonce():
    return secrets.token_urlsafe(32)


def grant(model, scope="tenant-a/orders", command="c1", payload="synthetic"):
    old, new = model.prepare(scope, command, payload)
    model.confirm(old, new, command)
    return old, new


def test_authority_first_is_pending_until_explicit_witness_confirmation(model):
    old, new = model.prepare("tenant-a/orders", "c1", "synthetic")
    with pytest.raises(Denied):
        model.validated_read(old.scope, nonce())
    with pytest.raises(Denied):
        model.prepare(old.scope, "c2", "other")
    model.confirm(old, new, "c1")
    assert model.validated_read(old.scope, nonce())[2:4] == (new.version, new.digest)


def test_commit_response_loss_recovers_only_exact_receipt_and_no_reexecution(model):
    old, new = model.prepare("tenant-a/orders", "c1", "synthetic")
    assert model.authority.receipt(old.scope, "c1") == (old.scope, "c1", new.version, new.digest)
    with pytest.raises(Denied):
        model.confirm(old, new, "wrong-command")
    with pytest.raises(Denied):
        model.prepare(old.scope, "c1", "synthetic")
    model.confirm(old, new, "c1")
    assert model.witness.receipt(old.scope, "c1") == (old.scope, "c1", new.version, new.digest)
    with pytest.raises(Denied):
        model.confirm(old, new, "c1")
    assert model.validated_read(old.scope, nonce())[2] == 1


def test_witness_response_loss_is_recovered_by_reads_not_repeated_update(model):
    old, new = grant(model)
    assert model.witness.receipt(old.scope, "c1") == model.authority.receipt(old.scope, "c1")
    with pytest.raises(Denied):
        model.confirm(old, new, "c1")
    assert model.validated_read(old.scope, nonce())[2] == new.version


@pytest.mark.parametrize("bad", ["nonce", "scope", "audience", "signature", "issuer"])
def test_proof_binding_and_signature_fail_closed(model, bad):
    grant(model)
    scope, challenge = "tenant-a/orders", nonce()
    authority_proof = model.authority.proof(scope, challenge)
    witness_proof = model.witness.proof(scope, challenge)
    if bad == "nonce":
        authority_proof = model.authority.proof(scope, nonce())
    elif bad == "scope":
        authority_proof = model.authority.proof("tenant-b/orders", challenge)
    elif bad == "audience":
        authority_proof = model.authority.proof(scope, challenge, aud="wrong")
    elif bad == "signature":
        authority_proof = model.witness.proof(scope, challenge)
    else:
        payload = jwt.decode(authority_proof, options={"verify_signature": False}) | {"iss": "wrong"}
        authority_proof = jwt.encode(payload, model.authority.key, algorithm="ES256")
    with pytest.raises(Denied):
        model.validated_read(scope, challenge, authority_proof, witness_proof)


def test_old_proof_cannot_be_replayed_under_new_challenge(model):
    grant(model)
    scope, old_nonce, new_nonce = "tenant-a/orders", nonce(), nonce()
    proof = model.authority.proof(scope, old_nonce)
    with pytest.raises(Denied):
        model.validated_read(scope, new_nonce, a_proof=proof)


@pytest.mark.parametrize("unavailable", ["authority", "witness"])
def test_either_source_unavailable_denies_even_with_previous_proofs(model, unavailable):
    grant(model)
    getattr(model, unavailable).available = False
    with pytest.raises(Denied):
        model.validated_read("tenant-a/orders", nonce())


def test_revoke_and_same_content_regrant_changes_t2_and_rejects_old_result(model):
    grant(model)
    scope = "tenant-a/orders"
    first = model.validated_read(scope, nonce())
    old, revocation = model.prepare(scope, "c2", "synthetic", active=False)
    model.confirm(old, revocation, "c2")
    with pytest.raises(Denied):
        model.validated_read(scope, nonce())
    old, regrant = model.prepare(scope, "c3", "synthetic")
    model.confirm(old, regrant, "c3")
    final = model.validated_read(scope, nonce())
    assert first != final and final[2] == 3
    with pytest.raises(Denied):
        model.release_result(first, final, ["synthetic-result"])
    assert model.release_result(final, final, ["synthetic-result"]) == ["synthetic-result"]


def test_witness_ahead_or_same_version_different_digest_denies(model):
    old, current = grant(model)
    with closing(sqlite3.connect(model.witness.path)) as db:
        db.execute("UPDATE heads SET digest=? WHERE scope=?", ("conflict", old.scope))
        db.commit()
    with pytest.raises(Denied):
        model.validated_read(old.scope, nonce())
    with closing(sqlite3.connect(model.witness.path)) as db:
        db.execute("UPDATE heads SET version=? WHERE scope=?", (current.version + 1, old.scope))
        db.commit()
    with pytest.raises(Denied):
        model.validated_read(old.scope, nonce())


def test_both_freshly_sign_same_stale_state_is_not_detectable_by_nonce(model):
    old, new = grant(model)
    old_a, old_w = model.authority.head(old.scope), model.witness.head(old.scope)
    next_old, next_new = model.prepare(old.scope, "c2", "new")
    model.confirm(next_old, next_new, "c2")
    for store, stale in ((model.authority, old_a), (model.witness, old_w)):
        with closing(sqlite3.connect(store.path)) as db:
            db.execute(
                "UPDATE heads SET version=?,digest=?,active=? WHERE scope=?", (stale.version, stale.digest, int(stale.active), stale.scope)
            )
            db.commit()
    # Deliberate negative control: same current nonce, both stores compromised or rolled back.
    assert model.validated_read(old.scope, nonce())[2] == new.version


def test_pending_scope_survives_reconstruction_and_requires_explicit_recovery(model):
    scope = "tenant-a/orders"
    old, committed = model.prepare(scope, "c1", "synthetic")

    def reopen(store):
        recovered = object.__new__(FakeStore)
        recovered.path, recovered.key, recovered.issuer, recovered.available = store.path, store.key, store.issuer, True
        return recovered

    recovered = Model(reopen(model.authority), reopen(model.witness))
    with pytest.raises(Denied):
        recovered.validated_read(scope, nonce())
    assert recovered.authority.receipt(scope, "c1") == (scope, "c1", committed.version, committed.digest)
    assert recovered.witness.receipt(scope, "c1") is None
    # The operator explicitly confirms exact evidence; model has no automatic recovery.
    recovered.confirm(old, committed, "c1")
    assert recovered.validated_read(scope, nonce())[2] == 1


def test_pending_scope_does_not_implicitly_freeze_other_scope(model):
    model.prepare("tenant-a/orders", "pending", "synthetic")
    _, other = grant(model, scope="tenant-b/orders", command="other")
    assert model.validated_read(other.scope, nonce())[2] == 1
    with pytest.raises(Denied):
        model.validated_read("tenant-a/orders", nonce())


def test_scope_epoch_conflict_and_wrong_recovery_evidence_fail_closed(model):
    old, new = model.prepare("tenant-a/orders", "c1", "synthetic")
    with pytest.raises(Denied):
        model.confirm(old, Head(new.scope, new.epoch, new.version, "fake", new.active), "c1")
    model.confirm(old, new, "c1")
    with closing(sqlite3.connect(model.witness.path)) as db:
        db.execute("UPDATE heads SET epoch=? WHERE scope=?", ("other-epoch", old.scope))
        db.commit()
    with pytest.raises(Denied):
        model.validated_read(old.scope, nonce())


def test_witness_ahead_cannot_be_automatically_reset(model):
    old, new = grant(model)
    with closing(sqlite3.connect(model.authority.path)) as db:
        db.execute("DELETE FROM heads WHERE scope=?", (old.scope,))
        db.commit()
    with pytest.raises(Denied):
        model.validated_read(old.scope, nonce())
    with pytest.raises(Denied):
        model.prepare(old.scope, "c2", "other")
    assert model.witness.head(old.scope) == new
