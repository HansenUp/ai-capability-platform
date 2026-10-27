"""排空就绪状态与下发时的授权角色。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("t_console_node") as batch:
        batch.add_column(sa.Column("ready", sa.Boolean(), nullable=False, server_default=sa.true()))
        batch.add_column(sa.Column("boot_id", sa.String(64), nullable=True))
    with op.batch_alter_table("t_console_node_command") as batch:
        batch.add_column(sa.Column("authorization_role", sa.String(32), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("t_console_node_command") as batch:
        batch.drop_column("authorization_role")
    with op.batch_alter_table("t_console_node") as batch:
        batch.drop_column("boot_id")
        batch.drop_column("ready")
