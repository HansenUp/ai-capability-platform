"""Run 持久化执行（S7）：t_run 增租约三列与 durable、Run 列表与 reaper 索引；新表 t_run_input、t_run_credential、t_tool_operation。

t_run.last_seq 自 0001 起即由事件日志追加同步维护，无需回填；旧 Run 的 durable 一律为 0（不被 reaper 续跑），
lease_epoch 为 0、无持有者。

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-18 00:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

# Alembic 用于串联迁移链的版本标识
revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _ts() -> sa.DateTime:
    """时间列类型：与 ORM 的 UTCDateTime 对齐（同 0001，刻意不引用 ORM 类型）。"""
    return sa.DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql")


def upgrade() -> None:
    """加列（带服务端默认值回填存量行）、建索引与三张新表。"""
    with op.batch_alter_table("t_run") as batch:
        batch.add_column(sa.Column("lease_owner", sa.String(128), nullable=True))
        batch.add_column(sa.Column("lease_epoch", sa.Integer(), nullable=False, server_default=sa.text("0")))
        batch.add_column(sa.Column("lease_expires_at", _ts(), nullable=True))
        batch.add_column(sa.Column("durable", sa.Boolean(), nullable=False, server_default=sa.text("0")))
    op.create_index("ix_run_tenant_subject_created", "t_run", ["tenant_id", "subject", "created_at"])
    op.create_index("ix_run_tenant_session_created", "t_run", ["tenant_id", "session_id", "created_at"])
    op.create_index("ix_run_durable_state", "t_run", ["durable", "state"])

    op.create_table(
        "t_run_input",
        sa.Column("run_id", sa.String(40), primary_key=True),
        sa.Column("messages", sa.JSON(), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("created_at", _ts(), nullable=False),
    )
    op.create_table(
        "t_run_credential",
        sa.Column("run_id", sa.String(40), primary_key=True),
        sa.Column("subject", sa.String(128), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("expires_at", _ts(), nullable=True),
    )
    op.create_table(
        "t_tool_operation",
        sa.Column("operation_id", sa.String(80), primary_key=True),
        sa.Column("run_id", sa.String(40), nullable=False),
        sa.Column("call_id", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("result_hash", sa.String(64), nullable=True),
        sa.Column("node", sa.String(128), nullable=True),
        sa.Column("epoch", sa.Integer(), nullable=False),
        sa.Column("created_at", _ts(), nullable=False),
    )
    op.create_index("ix_t_tool_operation_run_id", "t_tool_operation", ["run_id"])


def downgrade() -> None:
    """按创建的反序撤销。"""
    op.drop_index("ix_t_tool_operation_run_id", table_name="t_tool_operation")
    op.drop_table("t_tool_operation")
    op.drop_table("t_run_credential")
    op.drop_table("t_run_input")
    op.drop_index("ix_run_durable_state", table_name="t_run")
    op.drop_index("ix_run_tenant_session_created", table_name="t_run")
    op.drop_index("ix_run_tenant_subject_created", table_name="t_run")
    with op.batch_alter_table("t_run") as batch:
        batch.drop_column("durable")
        batch.drop_column("lease_expires_at")
        batch.drop_column("lease_epoch")
        batch.drop_column("lease_owner")
