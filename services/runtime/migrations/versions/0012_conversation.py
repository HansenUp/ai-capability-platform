"""Persist conversation ownership and registered continuation inputs."""

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "t_conversation",
        sa.Column("conversation_id", sa.String(36), primary_key=True),
        sa.Column("scope_id", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("subject", sa.String(128), nullable=False),
        sa.Column("agent_id", sa.String(64), nullable=False),
    )
    op.create_index("ix_t_conversation_scope_id", "t_conversation", ["scope_id"])
    op.create_table(
        "t_continuation_input",
        sa.Column("attempt_id", sa.String(36), primary_key=True),
        sa.Column("conversation_id", sa.String(36), nullable=False),
        sa.Column("run_id", sa.String(40), nullable=False, unique=True),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("selected_run_ids", sa.JSON(), nullable=False),
        sa.Column("composed", sa.Text(), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
    )
    op.create_index("ix_t_continuation_input_conversation_id", "t_continuation_input", ["conversation_id"])


def downgrade() -> None:
    op.drop_index("ix_t_continuation_input_conversation_id", table_name="t_continuation_input")
    op.drop_table("t_continuation_input")
    op.drop_index("ix_t_conversation_scope_id", table_name="t_conversation")
    op.drop_table("t_conversation")
