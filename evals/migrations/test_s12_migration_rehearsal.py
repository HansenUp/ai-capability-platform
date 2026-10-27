"""0005 到 0011 的合成数据迁移演练；不启动应用或 Worker。"""

import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from alembic import command
from sqlalchemy.engine import Connection

from runtime.state.db import alembic_config

NEW_TABLES = {"t_node_model_route", "t_node_command_receipt", "t_workbench_scope", "t_workbench_attempt", "t_feedback_outbox"}
STAMP = datetime(2026, 1, 2, 3, 4, 5, 123456)


def migrate(connection: Connection, target: str, *, down: bool = False) -> None:
    config = alembic_config()
    config.attributes["connection"] = connection
    if down:
        command.downgrade(config, target)
    else:
        command.upgrade(config, target)
    connection.commit()


def snapshot(connection: Connection, tables: set[str]) -> dict[str, Any]:
    inspector = sa.inspect(connection)
    result = {}
    for name in sorted(tables):
        table = sa.Table(name, sa.MetaData(), autoload_with=connection)
        rows = [dict(row) for row in connection.execute(sa.select(table).order_by(*table.primary_key.columns)).mappings()]
        encoded = json.dumps(rows, sort_keys=True, default=lambda value: value.hex() if isinstance(value, bytes) else str(value))
        result[name] = {
            "rows": len(rows),
            "sha256": hashlib.sha256(encoded.encode()).hexdigest(),
            "columns": [(column["name"], str(column["type"]), column["nullable"]) for column in inspector.get_columns(name)],
            "indexes": sorted(index["name"] for index in inspector.get_indexes(name)),
        }
    connection.commit()
    return result


def initialize_0005(connection: Connection) -> dict[str, Any]:
    assert not sa.inspect(connection).get_table_names()
    connection.commit()
    migrate(connection, "0005")
    tables = set(sa.inspect(connection).get_table_names()) - {"alembic_version"}
    for name in sorted(tables):
        table = sa.Table(name, sa.MetaData(), autoload_with=connection)
        values: dict[str, Any] = {}
        for column in table.columns:
            if column.nullable or column.server_default is not None or (column.primary_key and isinstance(column.type, sa.Integer)):
                continue
            if isinstance(column.type, sa.Boolean):
                value: Any = False
            elif isinstance(column.type, sa.Integer):
                value = 1
            elif isinstance(column.type, sa.JSON):
                value = {"synthetic": True}
            elif isinstance(column.type, sa.DateTime):
                value = STAMP
            elif isinstance(column.type, sa.LargeBinary):
                value = b"synthetic-not-a-real-credential"
            else:
                value = "synthetic"
            values[column.name] = value
        values.update(
            {
                key: value
                for key, value in {
                    "run_id": "synthetic-run",
                    "tenant_id": "synthetic-tenant",
                    "agent_id": "synthetic-agent",
                    "subject": "human:synthetic",
                    "state": "completed",
                    "last_seq": 1,
                }.items()
                if key in table.columns
            }
        )
        if name == "t_run_event":
            values.update(seq=1, type="run.state_changed", payload={"to": "completed"})
        if name == "t_agent_version":
            values.update(config={"extension": {"preserved": True}}, skills_lock=[{"name": "synthetic", "version": "1"}])
        connection.execute(table.insert().values(**values))
        if name == "t_agent_version":
            connection.execute(table.insert().values(**(values | {"agent_id": "synthetic-legacy-agent", "skills_lock": sa.null()})))
        if name == "t_run":
            connection.execute(
                table.insert().values(
                    **(
                        values
                        | {
                            "run_id": "synthetic-working",
                            "state": "working",
                            "durable": True,
                            "lease_owner": "synthetic-node",
                            "lease_epoch": 7,
                            "lease_expires_at": STAMP + timedelta(minutes=5),
                        }
                    )
                )
            )
        if name == "t_run_event":
            connection.execute(table.insert().values(**(values | {"run_id": "synthetic-working", "payload": {"to": "working"}})))
    connection.commit()
    return snapshot(connection, tables)


def exercise_upgrade(connection: Connection, baseline: dict[str, Any]) -> dict[str, Any]:
    migrate(connection, "0007")
    metadata = sa.MetaData()
    scope = sa.Table("t_workbench_scope", metadata, autoload_with=connection)
    attempts = sa.Table("t_workbench_attempt", metadata, autoload_with=connection)
    connection.execute(scope.insert().values(scope_id="synthetic-scope", active_attempt="synthetic-attempt", revision=1))
    connection.execute(
        attempts.insert().values(
            attempt_id="synthetic-attempt",
            scope_id="synthetic-scope",
            run_id="synthetic-run",
            request_seq=1,
            fingerprint="a" * 64,
            status="PREPARED",
            revision=1,
            acknowledged=False,
            created_at=STAMP,
        )
    )
    connection.commit()
    migrate(connection, "0011")
    assert snapshot(connection, set(baseline)) == baseline
    assert connection.execute(sa.text("SELECT COUNT(*) FROM t_agent_version WHERE skills_lock IS NULL")).scalar_one() == 1
    connection.commit()
    assert NEW_TABLES <= set(sa.inspect(connection).get_table_names())
    row = connection.execute(
        sa.text(
            "SELECT action, call_id, params_hash, value_hmac, create_agent, create_version, "
            "create_model, create_snapshot, create_reason FROM t_workbench_attempt"
        )
    ).one()
    assert tuple(row) == ("credential", None, None, None, None, None, None, None, None)
    connection.commit()
    unique = sa.inspect(connection).get_unique_constraints("t_feedback_outbox")
    assert any(item["name"] == "uq_feedback_run_revision" and item["column_names"] == ["run_id", "score_revision"] for item in unique)
    outbox = sa.Table("t_feedback_outbox", sa.MetaData(), autoload_with=connection)
    connection.execute(
        outbox.insert().values(
            attempt_id="synthetic-attempt",
            run_id="synthetic-run",
            score_revision=1,
            payload={"synthetic": True},
            payload_hash="b" * 64,
            observed_hash=None,
        )
    )
    connection.commit()
    upgraded = snapshot(connection, set(baseline) | NEW_TABLES)
    migrate(connection, "0011")
    assert snapshot(connection, set(baseline) | NEW_TABLES) == upgraded
    assert connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one() == "0011"
    connection.commit()
    return {
        "old_tables_preserved": len(baseline),
        "default_action": "credential",
        "new_nullable_fields_null": True,
        "head_upgrade_idempotent": True,
        "upgraded_snapshot": upgraded,
    }


def exercise_downgrade(connection: Connection, baseline: dict[str, Any]) -> None:
    migrate(connection, "0005", down=True)
    assert not NEW_TABLES & set(sa.inspect(connection).get_table_names())
    assert snapshot(connection, set(baseline)) == baseline
    assert connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one() == "0005"
    connection.commit()


def test_0005_0011_upgrade_downgrade_compatibility(tmp_path: Path) -> None:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'migration-rehearsal.db'}")
    try:
        with engine.connect() as connection:
            baseline = initialize_0005(connection)
            result = exercise_upgrade(connection, baseline)
            assert result["old_tables_preserved"] > 0
            exercise_downgrade(connection, baseline)
            exercise_upgrade(connection, baseline)
    finally:
        engine.dispose()
