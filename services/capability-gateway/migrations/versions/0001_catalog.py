"""能力目录：cg_capability / cg_agent_binding / cg_connector / cg_connector_tool / cg_outbox。

Revision ID: 0001
Revises:
Create Date: 2026-10-18 00:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """建表；字段类型、长度、可空性与 capability_gateway.db 中的 ORM 模型一致（test_catalog_sql 比对）。"""
    op.create_table(
        "cg_capability",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("tenant", sa.String(64), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("type", sa.String(16), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("input_schema", sa.JSON(), nullable=False),
        sa.Column("system", sa.String(64), nullable=False),
        sa.Column("owner", sa.String(128), nullable=False),
        sa.Column("version", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("risk_level", sa.String(16), nullable=False),
        sa.Column("data_class", sa.String(16), nullable=False),
        sa.Column("supports_dry_run", sa.Boolean(), nullable=False),
        sa.Column("requires_idempotency_key", sa.Boolean(), nullable=False),
        sa.Column("compensate_tool", sa.String(128), nullable=True),
        sa.Column("connector", sa.String(128), nullable=True),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("tenant", "name", name="uq_cg_capability_tenant_name"),
    )
    op.create_index("ix_cg_capability_status", "cg_capability", ["status"])
    op.create_table(
        "cg_agent_binding",
        sa.Column("agent_id", sa.String(64), primary_key=True),
        sa.Column("capability_id", sa.String(128), primary_key=True),
        sa.Column("kind", sa.String(16), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_cg_agent_binding_capability_id", "cg_agent_binding", ["capability_id"])
    op.create_table(
        "cg_connector",
        sa.Column("name", sa.String(128), primary_key=True),
        sa.Column("version", sa.String(32), primary_key=True),
        sa.Column("endpoint", sa.String(512), nullable=False),
        sa.Column("protocol", sa.String(16), nullable=False),
        sa.Column("auth_type", sa.String(16), nullable=False),
        sa.Column("system", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("health", sa.String(16), nullable=False),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.Column("manifest_hash", sa.String(64), nullable=True),
        sa.Column("last_probe_at", sa.Float(), nullable=True),
        sa.Column("last_error", sa.String(512), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_cg_connector_system", "cg_connector", ["system"])
    op.create_table(
        "cg_connector_tool",
        sa.Column("connector", sa.String(128), primary_key=True),
        sa.Column("version", sa.String(32), primary_key=True),
        sa.Column("name", sa.String(128), primary_key=True),
        sa.Column("spec", sa.JSON(), nullable=False),
        sa.Column("read_only", sa.Boolean(), nullable=False),
        sa.Column("errors", sa.JSON(), nullable=False),
    )
    op.create_table(
        "cg_outbox",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True),
        sa.Column("aggregate", sa.String(32), nullable=False),
        sa.Column("aggregate_id", sa.String(128), nullable=False),
        sa.Column("op", sa.String(16), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
    )
    op.create_index("ix_cg_outbox_created", "cg_outbox", ["created_at"])


def downgrade() -> None:
    op.drop_table("cg_outbox")
    op.drop_table("cg_connector_tool")
    op.drop_table("cg_connector")
    op.drop_table("cg_agent_binding")
    op.drop_table("cg_capability")
