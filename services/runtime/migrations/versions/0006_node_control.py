"""节点模型路由与幂等指令回执。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "t_node_model_route",
        sa.Column("node_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), primary_key=True),
        sa.Column("agent_id", sa.String(64), primary_key=True),
        sa.Column("base_version", sa.String(32), nullable=False),
        sa.Column("target_version", sa.String(32), nullable=False),
    )
    op.create_table(
        "t_node_command_receipt",
        sa.Column("node_id", sa.String(128), primary_key=True),
        sa.Column("command_id", sa.String(128), primary_key=True),
        sa.Column("args_hash", sa.String(64), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("t_node_command_receipt")
    op.drop_table("t_node_model_route")
