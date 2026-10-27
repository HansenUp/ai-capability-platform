"""预留创建范围和 Agent 快照绑定，不另存消息正文。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for name, length in (
        ("create_agent", 64),
        ("create_version", 32),
        ("create_model", 64),
        ("create_snapshot", 64),
        ("create_reason", 32),
    ):
        op.add_column("t_workbench_attempt", sa.Column(name, sa.String(length), nullable=True))


def downgrade() -> None:
    for name in ("create_reason", "create_snapshot", "create_model", "create_version", "create_agent"):
        op.drop_column("t_workbench_attempt", name)
