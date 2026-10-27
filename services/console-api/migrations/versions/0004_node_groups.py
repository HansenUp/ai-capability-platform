"""S10 分组元数据，保留现有节点标签与租户绑定。"""

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    table = op.create_table(
        "t_console_node_group",
        sa.Column("name", sa.String(64), primary_key=True),
        sa.Column("description", sa.String(500), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql"), nullable=False),
    )
    connection = op.get_bind()
    nodes = sa.table("t_console_node", sa.column("labels", sa.JSON()))
    bindings = sa.table("t_console_node_binding", sa.column("weights", sa.JSON()))
    names = {"general"}
    for labels in connection.execute(sa.select(nodes.c.labels)).scalars():
        names.update(labels)
    for weights in connection.execute(sa.select(bindings.c.weights)).scalars():
        names.update(weights)
    op.bulk_insert(table, [{"name": name, "description": "", "created_at": datetime.now(UTC)} for name in sorted(names)])


def downgrade() -> None:
    op.drop_table("t_console_node_group")
