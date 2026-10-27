"""Console-only display metadata; no identity or authorization lifecycle."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "t_console_directory_tenant",
        sa.Column("tenant_id", sa.String(64), primary_key=True),
        sa.Column("display_name", sa.String(128), nullable=False),
        sa.Column("description", sa.String(500), nullable=False),
        mysql_collate="utf8mb4_bin",
    )
    op.create_table(
        "t_console_directory_user",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("issuer", sa.String(255), nullable=False),
        sa.Column("subject", sa.String(128), nullable=False),
        sa.Column("display_name", sa.String(128), nullable=False),
        sa.Column("description", sa.String(500), nullable=False),
        sa.UniqueConstraint("tenant_id", "issuer", "subject", name="uq_console_directory_identity"),
        mysql_collate="utf8mb4_bin",
    )


def downgrade() -> None:
    op.drop_table("t_console_directory_user")
    op.drop_table("t_console_directory_tenant")
