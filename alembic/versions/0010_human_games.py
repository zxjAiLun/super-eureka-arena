"""human_games and human_game_moves

Revision ID: 0010_human_games
Revises: 0009_engine_versions
Create Date: 2026-08-27

Human-vs-engine play (dark launch). Two tables only:

- ``human_games``: one interactive game. The opponent engine launch
  configuration is frozen at creation into ``opponent_snapshot`` (display
  name, kind, preset/version/build identity, binary SHA, command args, UCI
  options) following the engine-version-identity ADR: a later preset edit or
  channel promotion never affects a game already in progress.
  ``revision`` is the optimistic-concurrency counter; ``engine_pending``
  marks that the worker still owes the engine reply. ``game_token_hash`` is
  the SHA-256 of the secret returned to the browser exactly once at
  creation.
- ``human_game_moves``: append-only move log, one row per ply with the FEN
  after the move and the engine's wall time.

State machine:
  ACTIVE -> FINISHED | EXPIRED | INTERRUPTED | ENGINE_FAILED | RESIGNED
  (terminal states are final; expiry is applied lazily on access).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010_human_games"
down_revision = "0009_engine_versions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "human_games",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("game_token_hash", sa.String(64), nullable=False, index=True),
        sa.Column("opponent_kind", sa.String(16), nullable=False),
        sa.Column("opponent_ref", sa.String(128), nullable=False),
        sa.Column("opponent_snapshot", sa.JSON(), nullable=False),
        sa.Column("human_color", sa.String(5), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("result", sa.String(16), nullable=True),
        sa.Column("termination", sa.String(64), nullable=True),
        sa.Column("current_fen", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("engine_pending", sa.Boolean(), nullable=False),
        sa.Column("creator_ip", sa.String(64), nullable=False),
        sa.Column("pgn_path", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_move_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "idle_expires_at", sa.DateTime(timezone=True), nullable=False
        ),
    )
    op.create_table(
        "human_game_moves",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "human_game_id",
            sa.String(36),
            sa.ForeignKey("human_games.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("ply", sa.Integer(), nullable=False),
        sa.Column("side", sa.String(6), nullable=False),
        sa.Column("uci", sa.String(8), nullable=False),
        sa.Column("san", sa.String(12), nullable=False),
        sa.Column("fen_after", sa.Text(), nullable=False),
        sa.Column("engine_ms", sa.Integer(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.UniqueConstraint("human_game_id", "ply", name="uq_human_moves_ply"),
    )


def downgrade() -> None:
    op.drop_table("human_game_moves")
    op.drop_table("human_games")
