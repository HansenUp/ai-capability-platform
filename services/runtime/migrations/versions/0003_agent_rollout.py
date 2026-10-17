"""哈希灰度（S3.2）：t_agent_version 增加放量比例与白名单。

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-17 00:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Alembic 用于串联迁移链的版本标识
revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """存量版本一律视为全量（100）、无白名单。JSON 列在 MySQL 下不能有字面默认值，先可空加列、回填后再置非空。"""
    with op.batch_alter_table("t_agent_version") as batch:
        batch.add_column(sa.Column("rollout_percent", sa.Integer(), nullable=False, server_default="100"))
        batch.add_column(sa.Column("rollout_allowlist", sa.JSON(), nullable=True))
    op.execute("UPDATE t_agent_version SET rollout_allowlist = '[]' WHERE rollout_allowlist IS NULL")
    with op.batch_alter_table("t_agent_version") as batch:
        batch.alter_column("rollout_allowlist", existing_type=sa.JSON(), nullable=False)
        batch.alter_column("rollout_percent", existing_type=sa.Integer(), server_default=None)


def downgrade() -> None:
    """删除两列。"""
    with op.batch_alter_table("t_agent_version") as batch:
        batch.drop_column("rollout_allowlist")
        batch.drop_column("rollout_percent")
