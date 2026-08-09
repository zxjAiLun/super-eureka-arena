"""tournaments.arena_elo_enabled

Revision ID: 0008_arena_elo_enabled
Revises: 0007_opening_set_format
Create Date: 2026-08-09

Adds the explicit "Rated match" opt-in for Arena Elo (P4.8).  Defaults to
False so smoke/UI matches never pollute ratings; the admin detail page can
flip it after completion.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008_arena_elo_enabled"
down_revision = "0007_opening_set_format"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tournaments",
        sa.Column("arena_elo_enabled", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("tournaments", "arena_elo_enabled")
