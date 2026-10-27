"""工作台单卡审批与拒绝的动作绑定；既有凭据记录保持兼容。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("t_workbench_attempt", sa.Column("action", sa.String(16), nullable=False, server_default="credential"))
    op.add_column("t_workbench_attempt", sa.Column("call_id", sa.String(128), nullable=True))
    op.add_column("t_workbench_attempt", sa.Column("params_hash", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("t_workbench_attempt", "params_hash")
    op.drop_column("t_workbench_attempt", "call_id")
    op.drop_column("t_workbench_attempt", "action")
