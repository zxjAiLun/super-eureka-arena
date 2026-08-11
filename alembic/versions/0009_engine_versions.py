"""engine_versions and engine_channels

Revision ID: 0009_engine_versions
Revises: 0008_arena_elo_enabled
Create Date: 2026-08-11

Adds the stable, immutable rated-engine identity (EngineVersion) and the
mutable production alias (EngineChannel). EngineVersion.version_id is the
Elo participant identity; its launch configuration (build_id, command_args,
uci_options, source_sha, binary_sha256) is snapshotted at creation and is
never dereferenced from a mutable EnginePreset afterwards.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009_engine_versions"
down_revision = "0008_arena_elo_enabled"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "engine_versions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("version_id", sa.String(), nullable=False, unique=True),
        sa.Column("display_name", sa.String(), nullable=False),
        sa.Column("build_id", sa.String(), nullable=False),
        sa.Column("command_args", sa.JSON(), nullable=False),
        sa.Column("uci_options", sa.JSON(), nullable=False),
        sa.Column("source_sha", sa.String(), nullable=False),
        sa.Column("binary_sha256", sa.String(64), nullable=False),
        sa.Column("identity_fingerprint", sa.String(64), nullable=False, unique=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column(
            "rating_enabled", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        sa.Column(
            "public_visible", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "engine_channels",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("channel_id", sa.String(), nullable=False, unique=True),
        sa.Column("engine_version_id", sa.String(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("engine_channels")
    op.drop_table("engine_versions")
