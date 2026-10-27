"""Skill 商店（S9）：cg_skill / cg_skill_version / cg_skill_event。

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-18 18:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """字段类型、长度、可空性与 capability_gateway.db 中的 ORM 模型一致（test_catalog_sql 比对）。"""
    op.create_table(
        "cg_skill",
        sa.Column("name", sa.String(64), primary_key=True),
        sa.Column("tenant", sa.String(64), nullable=False),
        sa.Column("created_by", sa.String(256), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_cg_skill_tenant", "cg_skill", ["tenant"])
    op.create_table(
        "cg_skill_version",
        sa.Column("name", sa.String(64), primary_key=True),
        sa.Column("version", sa.String(32), primary_key=True),
        sa.Column("tenant", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("manifest_sha256", sa.String(64), nullable=True),
        sa.Column("source_key", sa.String(512), nullable=False),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("package_key", sa.String(512), nullable=True),
        sa.Column("package_sha256", sa.String(64), nullable=True),
        sa.Column("scan_report", sa.JSON(), nullable=True),
        sa.Column("rules_version", sa.String(32), nullable=True),
        sa.Column("eval_run_ref", sa.String(256), nullable=True),
        sa.Column("eval_pass_rate", sa.Float(), nullable=True),
        sa.Column("eval_items", sa.Integer(), nullable=True),
        sa.Column("submitted_by", sa.String(256), nullable=False),
        sa.Column("reviewed_by", sa.String(256), nullable=True),
        sa.Column("signature", sa.JSON(), nullable=True),
        sa.Column("kid", sa.String(64), nullable=True),
        sa.Column("listed_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("revoke_reason", sa.String(512), nullable=True),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_cg_skill_version_status", "cg_skill_version", ["status"])
    op.create_table(
        "cg_skill_event",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("version", sa.String(32), nullable=False),
        sa.Column("event", sa.String(32), nullable=False),
        sa.Column("actor", sa.String(256), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_cg_skill_event_skill", "cg_skill_event", ["name", "version"])


def downgrade() -> None:
    op.drop_table("cg_skill_event")
    op.drop_table("cg_skill_version")
    op.drop_table("cg_skill")
