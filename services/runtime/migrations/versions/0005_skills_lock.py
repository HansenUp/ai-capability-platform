"""Skill 锁（S9 D143）：t_agent_version 增 skills_lock（JSON，可空）。

发布时经能力网关 POST /skills/resolve 校验 Agent 声明的 Skill（listed、同租户、声明能力 ⊆ Agent 工具 ∪ 发现范围）并冻结
[{name, version, manifest_sha256}]；S9 之前发布的版本保持 NULL（未冻结），不回填。

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-18 12:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Alembic 用于串联迁移链的版本标识
revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """加可空 JSON 列（存量行为 NULL）。"""
    with op.batch_alter_table("t_agent_version") as batch:
        batch.add_column(sa.Column("skills_lock", sa.JSON(), nullable=True))


def downgrade() -> None:
    """删列。"""
    with op.batch_alter_table("t_agent_version") as batch:
        batch.drop_column("skills_lock")
