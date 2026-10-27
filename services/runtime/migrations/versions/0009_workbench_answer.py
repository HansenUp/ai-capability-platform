"""输入补充只保存域隔离内容HMAC，不另存回答原文。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("t_workbench_attempt", sa.Column("value_hmac", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("t_workbench_attempt", "value_hmac")
