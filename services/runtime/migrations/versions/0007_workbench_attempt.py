"""工作台同主体写前登记与不可歧义回执。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "t_workbench_scope",
        sa.Column("scope_id", sa.String(64), primary_key=True),
        sa.Column("active_attempt", sa.String(36), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
    )
    op.create_table(
        "t_workbench_attempt",
        sa.Column("attempt_id", sa.String(36), primary_key=True),
        sa.Column("scope_id", sa.String(64), nullable=False),
        sa.Column("run_id", sa.String(40), nullable=False),
        sa.Column("request_seq", sa.Integer(), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("evidence_seq", sa.Integer(), nullable=True),
        sa.Column("rejection", sa.String(64), nullable=True),
        sa.Column("acknowledged", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_t_workbench_attempt_scope_id", "t_workbench_attempt", ["scope_id"])


def downgrade() -> None:
    op.drop_table("t_workbench_attempt")
    op.drop_table("t_workbench_scope")
