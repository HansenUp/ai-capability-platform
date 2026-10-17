"""统一审计（S2.3）：t_audit 增加身份列（act、auth_method、tenant_id）与按时间、按操作者查询的索引。

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-17 00:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Alembic 用于串联迁移链的版本标识
revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """新增三列（均可空，存量审计行不回填）与两个索引。SQLite 下用 batch 模式，保证列与索引变更可执行。"""
    with op.batch_alter_table("t_audit") as batch:
        batch.add_column(sa.Column("act", sa.String(128), nullable=True))
        batch.add_column(sa.Column("auth_method", sa.String(24), nullable=True))
        batch.add_column(sa.Column("tenant_id", sa.String(64), nullable=True))
    op.create_index("ix_t_audit_ts", "t_audit", ["ts"])
    op.create_index("ix_t_audit_actor", "t_audit", ["actor"])


def downgrade() -> None:
    """按反序删除索引与列。"""
    op.drop_index("ix_t_audit_actor", table_name="t_audit")
    op.drop_index("ix_t_audit_ts", table_name="t_audit")
    with op.batch_alter_table("t_audit") as batch:
        batch.drop_column("tenant_id")
        batch.drop_column("auth_method")
        batch.drop_column("act")
