"""Database models for the arena (section 9 of the spec).

Design notes:
- UUIDs are stored as TEXT (portable across SQLite).
- ``engine_builds`` and ``opening_sets`` are immutable registries; nothing in
  the runtime mutates the files they point at.
- ``events`` is written from day one so a v2 live-spectating layer can stream
  from it without schema changes.
- ``worker_state`` is an internal single-row heartbeat used by /health.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def coerce_utc(dt: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes; normalize them to UTC-aware."""
    if dt is None or dt.tzinfo is not None:
        return dt
    return dt.replace(tzinfo=timezone.utc)


def default_uuid() -> str:
    import uuid

    return str(uuid.uuid4())


# Tournament lifecycle (section 10.1)
DRAFT = "DRAFT"
QUEUED = "QUEUED"
RUNNING = "RUNNING"
# S4.3D: formal pentanomial SPRT terminal statuses.
SPRT_ACCEPT_H1 = "SPRT_ACCEPT_H1"
SPRT_ACCEPT_H0 = "SPRT_ACCEPT_H0"
SPRT_MAX_PAIRS = "SPRT_MAX_PAIRS"
PAUSING = "PAUSING"
PAUSED = "PAUSED"
COMPLETED = "COMPLETED"
FAILED = "FAILED"
CANCELLED = "CANCELLED"
TOURNAMENT_STATUSES = frozenset(
    {DRAFT, QUEUED, RUNNING, PAUSING, PAUSED, COMPLETED,
     FAILED, CANCELLED,
     # S4.3D: formal pentanomial SPRT terminal states.
     SPRT_ACCEPT_H1, SPRT_ACCEPT_H0, SPRT_MAX_PAIRS}
)

# Allowed transitions (section 10.1)
TOURNAMENT_TRANSITIONS = {
    DRAFT: {QUEUED},
    QUEUED: {RUNNING, CANCELLED},
    RUNNING: {
        PAUSING, COMPLETED, FAILED, CANCELLED,
        SPRT_ACCEPT_H1, SPRT_ACCEPT_H0, SPRT_MAX_PAIRS,
    },
    PAUSING: {PAUSED, COMPLETED, FAILED, CANCELLED},
    PAUSED: {QUEUED, CANCELLED},
    COMPLETED: set(),
    FAILED: set(),
    CANCELLED: set(),
    SPRT_ACCEPT_H1: set(),
    SPRT_ACCEPT_H0: set(),
    SPRT_MAX_PAIRS: set(),
}

# S4.3D + P4.11 commit 4 closure: the successful terminal statuses that carry
# a replayable result.  A match may end with the full requested schedule
# (COMPLETED) or early on a formal SPRT decision (ACCEPT_H1 / ACCEPT_H0 /
# MAX_PAIRS).  FAILED/CANCELLED carry no result to replay, so they are NOT
# part of this set.  Every public history/detail/replay/PGN/diagnostics/Live
# contract must use this set instead of hardcoding `status == COMPLETED`.
RESULT_TERMINAL_STATUSES = frozenset(
    {COMPLETED, SPRT_ACCEPT_H1, SPRT_ACCEPT_H0, SPRT_MAX_PAIRS}
)

# Every status where the match is over: a pinned Live page must never keep
# showing "live" (with browser Stockfish running) for any of these.
ENDED_STATUSES = frozenset(
    TOURNAMENT_STATUSES - {DRAFT, QUEUED, RUNNING, PAUSING, PAUSED}
)

# Pair job lifecycle (section 10.2)
PENDING = "PENDING"
RUNNING = "RUNNING"
VERIFYING = "VERIFYING"
COMPLETED = "COMPLETED"
FAILED = "FAILED"
INTERRUPTED = "INTERRUPTED"

PAIR_STATUSES = frozenset(
    {PENDING, RUNNING, VERIFYING, COMPLETED, FAILED, INTERRUPTED}
)


class EngineBuild(Base):
    __tablename__ = "engine_builds"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    build_id: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    engine_name: Mapped[str] = mapped_column(String, nullable=False)
    git_sha: Mapped[str] = mapped_column(String, nullable=False)
    binary_path: Mapped[str] = mapped_column(Text, nullable=False)
    binary_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    platform: Mapped[str] = mapped_column(String, nullable=False)
    supported_profiles: Mapped[list] = mapped_column(JSON, nullable=False)
    manifest: Mapped[dict] = mapped_column(JSON, nullable=False)
    # UCI capability schema probed from THIS exact binary (bound to
    # binary_sha256); None for builds that predate capability capture.
    uci_options_schema: Mapped[dict | None] = mapped_column(
        JSON, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )


class EnginePreset(Base):
    """A selectable engine configuration bound to a physical EngineBuild.

    Separates the immutable physical binary (EngineBuild) from the logical
    launch configuration: extra command-line args (e.g. ``--profile``) and
    UCI options (e.g. Stockfish ``UCI_LimitStrength``/``UCI_Elo``) emitted
    as ``option.<name>=<value>`` under cutechess ``-each``.
    """

    __tablename__ = "engine_presets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    preset_id: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    build_id: Mapped[str] = mapped_column(String, nullable=False)
    display_name: Mapped[str] = mapped_column(String, nullable=False)
    command_args: Mapped[list] = mapped_column(JSON, nullable=False)
    uci_options: Mapped[dict] = mapped_column(JSON, nullable=False)
    category: Mapped[str] = mapped_column(
        String, default="external", nullable=False
    )
    public_visible: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


# EngineVersion lifecycle statuses (S4.3E ADR: version == Elo participant).
ENGINE_VERSION_STATUSES = frozenset(
    {"candidate", "production", "historical", "experimental"}
)


class EngineVersion(Base):
    """Permanent immutable CHESS/LAUNCH identity == Elo participant.

    ``version_id`` is the rating participant identity (NOT display_name and
    NOT the anonymous fingerprint). Launch configuration is SNAPSHOTTED at
    creation (build_id, command_args, uci_options, source_sha,
    binary_sha256); later EnginePreset edits never affect it.

    V2.1 immutability contract: "immutable" covers exactly the identity
    fields above — they can never change after creation. The lifecycle
    metadata (``status``, ``public_visible``, ``rating_enabled``) is mutable
    but ONLY via the controlled promotion flow
    (``services.versions.promote_channel``): candidate (hidden, unrated)
    -> production (public, rated) -> historical. Never edit it ad hoc.
    The identity is WHO THE BINARY IS (source_sha/binary_sha256/launch
    config), not which semantic promote-commit it corresponds to.
    """

    __tablename__ = "engine_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version_id: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String, nullable=False)

    build_id: Mapped[str] = mapped_column(String, nullable=False)
    command_args: Mapped[list] = mapped_column(JSON, nullable=False)
    uci_options: Mapped[dict] = mapped_column(JSON, nullable=False)

    source_sha: Mapped[str] = mapped_column(String, nullable=False)
    binary_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    identity_fingerprint: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False
    )

    status: Mapped[str] = mapped_column(String, nullable=False)
    rating_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    public_visible: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class EngineChannel(Base):
    """Mutable alias (e.g. ``current-final``) pointing at one EngineVersion.

    Promotion = repoint the channel via the ATOMIC ``promote_channel`` flow
    (old production → historical, target → production/public/rated, channel
    repoint — one transaction, all-or-nothing). The channel itself is not a
    participant and carries no rating. Existing tournaments/HumanGames hold
    frozen snapshots, so a promotion only affects the NEXT creation through
    the channel.
    """

    __tablename__ = "engine_channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel_id: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    engine_version_id: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )



class OpeningSet(Base):
    __tablename__ = "opening_sets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    opening_set_id: Mapped[str] = mapped_column(
        String, unique=True, nullable=False
    )
    file_path: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    position_count: Mapped[int] = mapped_column(Integer, nullable=False)
    # Book format: "epd" (legacy single-line positions) or "pgn" (multi-game
    # book such as the official Stockfish 8moves_v3 suite).
    format: Mapped[str] = mapped_column(String, default="epd", nullable=False)
    # Source provenance (repository/commit/license) for auditable books.
    source: Mapped[str | None] = mapped_column(Text, nullable=True)
    manifest: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )


class Tournament(Base):
    __tablename__ = "tournaments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=default_uuid)
    name: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, default=DRAFT, nullable=False)

    engine_a_build_id: Mapped[str] = mapped_column(String, nullable=False)
    engine_a_profile: Mapped[str] = mapped_column(String, nullable=False)
    engine_b_build_id: Mapped[str] = mapped_column(String, nullable=False)
    engine_b_profile: Mapped[str] = mapped_column(String, nullable=False)
    # P4.2: presets are the selectable unit; the build/profile columns above
    # remain as historical audit fields.
    engine_a_preset_id: Mapped[str | None] = mapped_column(
        String, nullable=True
    )
    engine_b_preset_id: Mapped[str | None] = mapped_column(
        String, nullable=True
    )
    opening_set_id: Mapped[str] = mapped_column(String, nullable=False)
    time_control: Mapped[str] = mapped_column(String, nullable=False)

    requested_pairs: Mapped[int] = mapped_column(Integer, nullable=False)
    completed_pairs: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    candidate_wins: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    candidate_losses: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    draws: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # P4.8: explicit opt-in for Arena Elo.  Default False so smoke/UI matches
    # never pollute ratings; flippable from the admin detail page.
    arena_elo_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    pause_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # P1.3: cross-process force-cancel flag.  The API sets it in the database;
    # the worker polls it, kills the process group, and only then marks the
    # tournament CANCELLED.  In-process shared memory is NOT used because the
    # API and worker are separate systemd processes.
    force_cancel_requested: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )

    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    config_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)

    pair_jobs: Mapped[list["PairJob"]] = relationship(
        back_populates="tournament",
        order_by="PairJob.pair_index",
        cascade="all, delete-orphan",
    )
    games: Mapped[list["Game"]] = relationship(back_populates="tournament")
    events: Mapped[list["Event"]] = relationship(back_populates="tournament")


class PairJob(Base):
    __tablename__ = "pair_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=default_uuid)
    tournament_id: Mapped[str] = mapped_column(
        ForeignKey("tournaments.id"), index=True, nullable=False
    )
    pair_index: Mapped[int] = mapped_column(Integer, nullable=False)
    opening_index: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String, default=PENDING, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    engine_a_white_game_id: Mapped[str | None] = mapped_column(String, nullable=True)
    engine_a_black_game_id: Mapped[str | None] = mapped_column(String, nullable=True)

    run_directory: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    verification: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # P1.5: the cutechess process exit code for this attempt (None while
    # pending/running).  A non-zero exit code fails the pair and the
    # tournament even when the artifacts look complete.
    return_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # The attempt the recorded exit code belongs to (P1: exit evidence must
    # belong to the CURRENT attempt; both are cleared on every retry).
    return_code_attempt: Mapped[int | None] = mapped_column(Integer, nullable=True)

    tournament: Mapped["Tournament"] = relationship(back_populates="pair_jobs")

    # Keep pair jobs ordered per tournament.
    __table_args__ = (
        # A pair can be retried via attempt, but only one live attempt at a
        # time for a given pair index.  Enforced by the worker, not the DB.
        None,
    )


class Game(Base):
    __tablename__ = "games"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=default_uuid)
    tournament_id: Mapped[str] = mapped_column(
        ForeignKey("tournaments.id"), index=True, nullable=False
    )
    pair_job_id: Mapped[str | None] = mapped_column(
        ForeignKey("pair_jobs.id"), nullable=True
    )
    game_number: Mapped[int] = mapped_column(Integer, nullable=False)
    white_engine: Mapped[str] = mapped_column(String, nullable=False)
    black_engine: Mapped[str] = mapped_column(String, nullable=False)
    opening_index: Mapped[int] = mapped_column(Integer, nullable=False)
    result: Mapped[str | None] = mapped_column(String, nullable=True)
    termination: Mapped[str | None] = mapped_column(String, nullable=True)
    pgn_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    tournament: Mapped["Tournament"] = relationship(back_populates="games")
    pair_job: Mapped["PairJob | None"] = relationship()


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tournament_id: Mapped[str] = mapped_column(
        ForeignKey("tournaments.id"), index=True, nullable=False
    )
    pair_job_id: Mapped[str | None] = mapped_column(String, nullable=True)
    game_id: Mapped[str | None] = mapped_column(String, nullable=True)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    tournament: Mapped["Tournament"] = relationship(back_populates="events")


# Human-vs-engine game lifecycle (dark-launch feature).
HUMAN_GAME_ACTIVE = "ACTIVE"
HUMAN_GAME_FINISHED = "FINISHED"          # natural termination (mate/stale/...)
HUMAN_GAME_RESIGNED = "RESIGNED"          # human resigned
HUMAN_GAME_EXPIRED = "EXPIRED"            # TTL or idle timeout, applied lazily
HUMAN_GAME_INTERRUPTED = "INTERRUPTED"    # abandoned after engine failures
HUMAN_GAME_ENGINE_FAILED = "ENGINE_FAILED"  # engine error ended the game
HUMAN_GAME_STATUSES = frozenset(
    {
        HUMAN_GAME_ACTIVE,
        HUMAN_GAME_FINISHED,
        HUMAN_GAME_RESIGNED,
        HUMAN_GAME_EXPIRED,
        HUMAN_GAME_INTERRUPTED,
        HUMAN_GAME_ENGINE_FAILED,
    }
)
HUMAN_GAME_TERMINAL_STATUSES = frozenset(
    {
        HUMAN_GAME_FINISHED,
        HUMAN_GAME_RESIGNED,
        HUMAN_GAME_EXPIRED,
        HUMAN_GAME_INTERRUPTED,
        HUMAN_GAME_ENGINE_FAILED,
    }
)


class HumanGame(Base):
    """One interactive human-vs-engine game (anonymous, token-authorized).

    The opponent launch configuration is frozen into ``opponent_snapshot``
    at creation (display_name, kind, preset/version/build ids, binary SHA,
    command_args, uci_options) so later preset edits or channel promotions
    never drift an in-progress game (engine-version-identity ADR).

    ``game_token_hash`` stores the SHA-256 of a secret handed to the browser
    exactly once at creation; every subsequent request must present it via
    the ``X-Game-Token`` header (constant-time compare).

    ``revision`` is the optimistic-concurrency counter: each accepted move
    (human or engine) increments it, and move submissions must echo the
    revision they were built against. ``engine_pending`` marks that the
    worker still owes the engine reply after the latest human move.

    Expiry is lazy: ``expires_at`` (absolute TTL) and ``idle_expires_at``
    (no-move timeout) are checked whenever the game is accessed; there is no
    background reaper because no engine process ever outlives a move.
    """

    __tablename__ = "human_games"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=default_uuid
    )
    game_token_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    opponent_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    opponent_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    opponent_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    human_color: Mapped[str] = mapped_column(String(5), nullable=False)  # white|black
    status: Mapped[str] = mapped_column(
        String(16), default=HUMAN_GAME_ACTIVE, nullable=False
    )
    # Checkmate/draw result in chess.py outcome terms: "1-0" | "0-1" | "1/2-1/2";
    # resigned games record the awarded result too ("0-1" when human is white).
    result: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Machine-readable termination: checkmate | stalemate | ...
    # resign | ttl_expired | idle_expired | engine_error | adjudicated
    termination: Mapped[str | None] = mapped_column(String(64), nullable=True)
    current_fen: Mapped[str] = mapped_column(Text, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    engine_pending: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    creator_ip: Mapped[str] = mapped_column(String(64), nullable=False)
    # PGN artifact once the game is terminal (under run_root/human-games/).
    pgn_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    last_move_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    idle_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    moves: Mapped[list["HumanGameMove"]] = relationship(
        back_populates="human_game",
        order_by="HumanGameMove.ply",
        cascade="all, delete-orphan",
    )


class HumanGameMove(Base):
    """Append-only ply log for one human game."""

    __tablename__ = "human_game_moves"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    human_game_id: Mapped[str] = mapped_column(
        ForeignKey("human_games.id"), index=True, nullable=False
    )
    ply: Mapped[int] = mapped_column(Integer, nullable=False)  # 1-based
    side: Mapped[str] = mapped_column(String(6), nullable=False)  # human|engine
    uci: Mapped[str] = mapped_column(String(8), nullable=False)
    san: Mapped[str] = mapped_column(String(12), nullable=False)
    fen_after: Mapped[str] = mapped_column(Text, nullable=False)
    # Wall-clock milliseconds the engine spent (engine rows only).
    engine_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    human_game: Mapped["HumanGame"] = relationship(back_populates="moves")

    __table_args__ = (
        UniqueConstraint("human_game_id", "ply", name="uq_human_moves_ply"),
    )


class WorkerState(Base):
    """Single-row heartbeat written by the worker process (internal).

    ``pid`` / ``pid_start_marker`` / ``pid_cmdline`` record the identity of the
    currently supervised cutechess process.  The identity is written at launch
    time (not just on heartbeat) so recovery can safely terminate an orphaned
    process after an abnormal worker death without risking PID reuse.
    """

    __tablename__ = "worker_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String, default="idle", nullable=False)
    heartbeat_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tournament_id: Mapped[str | None] = mapped_column(String, nullable=True)
    pair_job_id: Mapped[str | None] = mapped_column(String, nullable=True)
    pid_start_marker: Mapped[str | None] = mapped_column(String, nullable=True)
    pid_cmdline: Mapped[str | None] = mapped_column(Text, nullable=True)
