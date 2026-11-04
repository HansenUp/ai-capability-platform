"""Real core Runtime models in disposable SQLite, without a Worker or real identity."""

import asyncio
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

import pytest
from scripts.verify_local_restore import backup_snapshot, exercise, restore_snapshot, validate_owned_root


def test_wal_restore_preserves_core_receipts_locks_and_final_result(tmp_path):
    with tempfile.TemporaryDirectory(prefix="aip-local-restore-", dir=tmp_path) as name:
        result = asyncio.run(exercise(Path(name)))
        assert result["status"] == "PASS"
        assert result["checked_states"] == ["PREPARED", "COMMITTED", "REJECTED_NO_EFFECT"]
        assert result["restored_snapshot_is_latest_authority"] is False
        assert result["unresolved_locks_preserved"] is True
        assert result["no_automatic_execution_or_ACK"] is True
        assert result["foreign_scope_denied"] is True
    assert not Path(name).exists()


def test_existing_files_and_symlink_roots_are_not_used(tmp_path):
    occupied = tmp_path / "occupied"
    occupied.mkdir(mode=0o700)
    (occupied / "unrelated").write_text("preserve")
    with pytest.raises(ValueError):
        validate_owned_root(occupied)
    empty = tmp_path / "empty"
    empty.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(empty)
    with pytest.raises(ValueError):
        validate_owned_root(link)
    assert (occupied / "unrelated").read_text() == "preserve"


def test_corrupted_or_stale_digest_cannot_create_restored_database(tmp_path):
    source, snapshot, destination = (tmp_path / name for name in ["source.sqlite", "snapshot.sqlite", "restored.sqlite"])
    with closing(sqlite3.connect(source)) as connection:
        connection.execute("CREATE TABLE public_fixture(value TEXT)")
        connection.execute("INSERT INTO public_fixture VALUES ('synthetic')")
        connection.commit()
    expected = backup_snapshot(source, snapshot)
    data = snapshot.read_bytes()
    snapshot.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
    with pytest.raises(ValueError, match="digest mismatch"):
        restore_snapshot(snapshot, expected, destination)
    assert not destination.exists()


def test_backup_and_restore_refuse_existing_targets(tmp_path):
    source, snapshot, destination = (tmp_path / name for name in ["source.sqlite", "snapshot.sqlite", "restored.sqlite"])
    with closing(sqlite3.connect(source)) as connection:
        connection.execute("CREATE TABLE public_fixture(value INTEGER)")
        connection.commit()
    digest = backup_snapshot(source, snapshot)
    with pytest.raises(ValueError, match="overwrite"):
        backup_snapshot(source, snapshot)
    destination.write_text("unrelated file")
    with pytest.raises(ValueError, match="overwrite"):
        restore_snapshot(snapshot, digest, destination)
    assert destination.read_text() == "unrelated file"
