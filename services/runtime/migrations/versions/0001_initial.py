"""初始 schema：Agent 版本、Run 元信息、Run 事件日志、审计四张表。

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
    """时间列类型：与 ORM 的 UTCDateTime 对齐——通用为 DateTime(timezone=True)，MySQL 为 DATETIME(6)（保留微秒）。
    迁移脚本刻意不引用 ORM 中的类型，保证历史迁移不随后续模型改动而变化。"""
    return sa.DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql")


def upgrade() -> None:
    """创建四张表及其索引、唯一约束；字段类型、长度、可空性与 runtime.state.db 中的 ORM 模型一致。"""
    # Agent 已发布快照（配置）
    op.create_table(
        "t_agent_version",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("agent_id", sa.String(64), nullable=False),
        sa.Column("version", sa.String(32), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("model_alias", sa.String(64), nullable=False),
        sa.Column("harness", sa.String(32), nullable=True),
        sa.Column("system_prompt", sa.Text(), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("harness_version", sa.String(32), nullable=True),
        sa.Column("model_resolved", sa.String(128), nullable=True),
        sa.Column("skills_digest", sa.String(64), nullable=True),
        sa.Column("tools_schema_version", sa.String(32), nullable=True),
        sa.Column("retrieval_config", sa.JSON(), nullable=True),
        sa.Column("eval_run_ref", sa.String(128), nullable=True),
        sa.Column("eval_passed", sa.Boolean(), nullable=True),
        sa.Column("created_at", _ts(), nullable=False),
        sa.UniqueConstraint("agent_id", "version", name="uq_agent_version"),
    )
    op.create_index("ix_t_agent_version_agent_id", "t_agent_version", ["agent_id"])

    # Run 元信息（当前状态快照）
    op.create_table(
        "t_run",
        sa.Column("run_id", sa.String(40), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("agent_id", sa.String(64), nullable=False),
        sa.Column("agent_version", sa.String(32), nullable=False),
        sa.Column("model_alias", sa.String(64), nullable=False),
        sa.Column("subject", sa.String(128), nullable=False),
        sa.Column("session_id", sa.String(40), nullable=True),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("last_seq", sa.Integer(), nullable=False),
        sa.Column("trace_id", sa.String(64), nullable=True),
        sa.Column("created_at", _ts(), nullable=False),
        sa.Column("finished_at", _ts(), nullable=True),
    )
    op.create_index("ix_t_run_agent_id", "t_run", ["agent_id"])
    op.create_index("ix_t_run_subject", "t_run", ["subject"])
    op.create_index("ix_t_run_session_id", "t_run", ["session_id"])
    op.create_index("ix_t_run_state", "t_run", ["state"])

    # Run 追加写事件日志：联合主键 (run_id, seq)
    op.create_table(
        "t_run_event",
        sa.Column("run_id", sa.String(40), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False, autoincrement=False),
        sa.Column("type", sa.String(40), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("idempotency_key", sa.String(80), nullable=True),
        sa.Column("ts", _ts(), nullable=False),
        sa.PrimaryKeyConstraint("run_id", "seq"),
    )
    op.create_index("ix_run_event_idem", "t_run_event", ["idempotency_key"])

    # 管理操作审计
    op.create_table(
        "t_audit",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("ts", _ts(), nullable=False),
        sa.Column("actor", sa.String(128), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("object", sa.String(128), nullable=False),
        sa.Column("result", sa.String(16), nullable=False),
        sa.Column("trace_id", sa.String(64), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=False),
    )
    op.create_index("ix_t_audit_action", "t_audit", ["action"])


def downgrade() -> None:
    """按创建的反序删除索引与表（四张表之间无外键，反序删除即可）。"""
    op.drop_index("ix_t_audit_action", table_name="t_audit")
    op.drop_table("t_audit")
    op.drop_index("ix_run_event_idem", table_name="t_run_event")
    op.drop_table("t_run_event")
    op.drop_index("ix_t_run_state", table_name="t_run")
    op.drop_index("ix_t_run_session_id", table_name="t_run")
    op.drop_index("ix_t_run_subject", table_name="t_run")
    op.drop_index("ix_t_run_agent_id", table_name="t_run")
    op.drop_table("t_run")
    op.drop_index("ix_t_agent_version_agent_id", table_name="t_agent_version")
    op.drop_table("t_agent_version")
