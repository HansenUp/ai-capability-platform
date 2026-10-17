"""Console 初始 schema（S3.3）：agents、agent_versions、visibility、user_agents、audit 五张表。

Revision ID: 0001
Revises:
Create Date: 2026-10-17 00:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

# Alembic 用于串联迁移链的版本标识
revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _ts() -> sa.DateTime:
    """时间列：通用 DateTime(timezone=True)，MySQL 为 DATETIME(6)；与 ORM 的 UTCDateTime 对齐。"""
    return sa.DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql")


def upgrade() -> None:
    op.create_table(
        "t_console_agent",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("agent_id", sa.String(64), nullable=False),
        sa.Column("created_by", sa.String(128), nullable=False),
        sa.Column("created_at", _ts(), nullable=False),
        sa.UniqueConstraint("tenant_id", "agent_id", name="uq_console_agent"),
    )
    op.create_table(
        "t_console_agent_version",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("agent_id", sa.String(64), nullable=False),
        sa.Column("version", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("system_prompt", sa.Text(), nullable=False),
        sa.Column("model_alias", sa.String(64), nullable=False),
        sa.Column("harness", sa.String(32), nullable=True),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("eval_run_ref", sa.String(128), nullable=True),
        sa.Column("canary_percent", sa.Integer(), nullable=False),
        sa.Column("canary_allowlist", sa.JSON(), nullable=False),
        sa.Column("created_at", _ts(), nullable=False),
        sa.Column("updated_at", _ts(), nullable=False),
        sa.UniqueConstraint("tenant_id", "agent_id", "version", name="uq_console_agent_version"),
    )
    op.create_table(
        "t_console_visibility",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("agent_id", sa.String(64), nullable=False),
        sa.Column("groups", sa.JSON(), nullable=False),
        sa.Column("roles", sa.JSON(), nullable=False),
        sa.Column("allowlist", sa.JSON(), nullable=False),
        sa.UniqueConstraint("tenant_id", "agent_id", name="uq_console_visibility"),
    )
    op.create_table(
        "t_console_user_agent",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("user", sa.String(128), nullable=False),
        sa.Column("agent_id", sa.String(64), nullable=False),
        sa.UniqueConstraint("tenant_id", "user", "agent_id", name="uq_console_user_agent"),
    )
    op.create_table(
        "t_console_audit",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("ts", _ts(), nullable=False),
        sa.Column("actor", sa.String(128), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("object", sa.String(128), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=False),
    )
    op.create_index("ix_t_console_audit_ts", "t_console_audit", ["ts"])
    op.create_index("ix_t_console_audit_action", "t_console_audit", ["action"])


def downgrade() -> None:
    op.drop_index("ix_t_console_audit_action", table_name="t_console_audit")
    op.drop_index("ix_t_console_audit_ts", table_name="t_console_audit")
    for table in ("t_console_audit", "t_console_user_agent", "t_console_visibility", "t_console_agent_version", "t_console_agent"):
        op.drop_table(table)
