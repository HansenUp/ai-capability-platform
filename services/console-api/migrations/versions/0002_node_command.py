"""节点指令表（S9.8）：指令带 id 与回执，拉取不清空队列。

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-18 00:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _ts() -> sa.DateTime:
    return sa.DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql")


def upgrade() -> None:
    op.create_table(
        "t_console_node_command",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("command_id", sa.String(36), nullable=False),
        sa.Column("node_id", sa.String(128), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("type", sa.String(32), nullable=False),
        sa.Column("args", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("created_by", sa.String(128), nullable=False),
        sa.Column("created_at", _ts(), nullable=False),
        sa.Column("acked_at", _ts(), nullable=True),
        sa.UniqueConstraint("command_id"),
    )
    op.create_index("ix_t_console_node_command_node_status", "t_console_node_command", ["node_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_t_console_node_command_node_status", table_name="t_console_node_command")
    op.drop_table("t_console_node_command")
