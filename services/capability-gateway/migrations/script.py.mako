"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
${imports if imports else ""}
# Alembic 用于串联迁移链的版本标识
revision: str = ${repr(up_revision)}
down_revision: str | Sequence[str] | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


def upgrade() -> None:
    """升级：把 schema 从上一版本迁移到本版本。"""
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    """降级：撤销本版本的 schema 变更，回到上一版本。"""
    ${downgrades if downgrades else "pass"}
