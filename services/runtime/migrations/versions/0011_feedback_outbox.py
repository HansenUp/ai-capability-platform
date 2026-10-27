"""可信反馈的最小交付意图与只读观测证据，不存评论。"""

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "t_feedback_outbox",
        sa.Column("attempt_id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(40), nullable=False),
        sa.Column("score_revision", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("observed_hash", sa.String(64), nullable=True),
        sa.UniqueConstraint("run_id", "score_revision", name="uq_feedback_run_revision"),
    )


def downgrade() -> None:
    op.drop_table("t_feedback_outbox")
