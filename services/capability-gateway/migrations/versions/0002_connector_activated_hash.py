"""cg_connector.activated_manifest_hash：激活时的 manifest 哈希（S8 评审修复：/invoke 拒绝 manifest 漂移）。

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-18 12:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """存量 active 版本该列为空，首次探活成功时记录（TOFU，见 connectors.probe）。"""
    op.add_column("cg_connector", sa.Column("activated_manifest_hash", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("cg_connector", "activated_manifest_hash")
