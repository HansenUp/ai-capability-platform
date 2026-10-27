"""S10 节点与租户路由绑定持久化。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "t_console_node",
        sa.Column("node_id", sa.String(128), primary_key=True),
        sa.Column("topology", sa.String(16), nullable=False),
        sa.Column("labels", sa.JSON(), nullable=False),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql"), nullable=False),
        sa.Column("load", sa.Float(), nullable=False),
        sa.Column("qps", sa.Float(), nullable=False),
    )
    op.create_index("ix_t_console_node_last_seen", "t_console_node", ["last_seen"])
    op.create_table(
        "t_console_node_binding",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("agent_id", sa.String(64), nullable=False),
        sa.Column("weights", sa.JSON(), nullable=False),
        sa.UniqueConstraint("tenant_id", "agent_id", name="uq_console_node_binding"),
    )


def downgrade() -> None:
    op.drop_table("t_console_node_binding")
    op.drop_index("ix_t_console_node_last_seen", table_name="t_console_node")
    op.drop_table("t_console_node")
