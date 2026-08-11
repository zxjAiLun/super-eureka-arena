"""Pydantic request / response schemas for the arena API (section 16)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from .config import TIME_CONTROLS

ALLOWED_TIME_CONTROLS = set(TIME_CONTROLS.keys())


# ---------------------------------------------------------------------------
# Builds / opening sets
# ---------------------------------------------------------------------------
class BuildOut(BaseModel):
    build_id: str
    engine_name: str
    git_sha: str
    binary_path: str
    binary_sha256: str
    platform: str
    supported_profiles: List[str]
    created_at: datetime
    enabled: bool

    model_config = {"from_attributes": True}


class OpeningSetOut(BaseModel):
    opening_set_id: str
    file_path: str
    sha256: str
    position_count: int
    created_at: datetime
    enabled: bool

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Tournaments
# ---------------------------------------------------------------------------
class EngineRef(BaseModel):
    preset_id: str = Field(default="", min_length=0)
    # S4.3E Phase 1: stable rated identity side (version == Elo participant).
    # When set, the tournament freezes the EngineVersion launch snapshot;
    # custom_elo is not allowed with version selection.
    version_id: Optional[str] = Field(default=None, min_length=1)
    # P4.6: per-match UCI_Elo override for the selected preset's engine build.
    # None keeps the preset exactly as registered; a value is validated against
    # the build's probed capability schema (UCI_Elo spin min/max +
    # UCI_LimitStrength check) at creation and frozen into the snapshot.
    custom_elo: Optional[int] = Field(default=None, ge=1)


class SprtConfig(BaseModel):
    """S4.3D frozen formal pentanomial SPRT contract.

    Stored verbatim into the tournament's frozen ``config_snapshot``; no live
    parameters are editable after the tournament starts.
    """

    enabled: bool = True
    unit: str = "pair"
    model: str = "pentanomial"
    elo_model: str = "logistic"
    elo0: float = 10.0
    elo1: float = 30.0
    alpha: float = 0.05
    beta: float = 0.05
    lower_bound: Optional[float] = None
    upper_bound: Optional[float] = None
    max_pairs: int = Field(ge=1)


class EngineVersionCreate(BaseModel):
    """S4.3E Phase 1: create an immutable EngineVersion (version == Elo
    participant). Exactly one of build_id (production/default artifact mode)
    or preset_id (historical/experimental snapshot mode) is required."""

    version_id: str = Field(min_length=1, max_length=100)
    display_name: str = Field(min_length=1, max_length=200)
    build_id: Optional[str] = Field(default=None, min_length=1)
    preset_id: Optional[str] = Field(default=None, min_length=1)
    command_args: Optional[list[str]] = None
    uci_options: Optional[dict] = None
    status: str = Field(default="candidate", pattern="^(candidate|production|historical|experimental)$")
    rating_enabled: bool = True
    public_visible: bool = True


class EngineVersionOut(BaseModel):
    version_id: str
    display_name: str
    build_id: str
    command_args: list
    uci_options: dict
    source_sha: str
    binary_sha256: str
    identity_fingerprint: str
    status: str
    rating_enabled: bool
    public_visible: bool
    created_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class EngineChannelUpdate(BaseModel):
    engine_version_id: str = Field(min_length=1)


class EngineChannelOut(BaseModel):
    channel_id: str
    engine_version_id: str
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class TournamentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    engine_a: EngineRef
    engine_b: EngineRef
    opening_set_id: str = Field(min_length=1)
    time_control: str
    pairs: int = Field(ge=1)
    allow_intentional_self_play: bool = False
    # P4.8: explicit opt-in for Arena Elo (default False; smoke never rated).
    arena_elo_enabled: bool = False
    # Phase C: PGN book depth (plies) and deterministic selection seed.
    opening_plies: Optional[int] = Field(default=None, ge=1)
    opening_seed: Optional[int] = Field(default=None, ge=0)
    # S4.3D: optional frozen SPRT contract (formal promotion test).
    sprt: Optional[SprtConfig] = None
    # S4.3D: normalized starting FENs to exclude from the opening sample
    # (prior tournaments must not leak into the formal test).
    opening_exclude_fens: Optional[list[str]] = None


class GameOut(BaseModel):
    id: str
    game_number: int
    white_engine: str
    black_engine: str
    opening_index: int
    result: Optional[str] = None
    termination: Optional[str] = None
    verified: bool
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class PairJobOut(BaseModel):
    id: str
    tournament_id: str
    pair_index: int
    opening_index: int
    status: str
    attempt: int
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    failure_reason: Optional[str] = None

    model_config = {"from_attributes": True}


class EventOut(BaseModel):
    id: int
    tournament_id: str
    pair_job_id: Optional[str] = None
    game_id: Optional[str] = None
    event_type: str
    payload: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime

    model_config = {"from_attributes": True}


class TournamentOut(BaseModel):
    id: str
    name: str
    status: str
    engine_a_build_id: str
    engine_a_profile: str
    engine_b_build_id: str
    engine_b_profile: str
    engine_a_preset_id: Optional[str] = None
    engine_b_preset_id: Optional[str] = None
    opening_set_id: str
    time_control: str
    requested_pairs: int
    completed_pairs: int
    candidate_wins: int
    candidate_losses: int
    draws: int
    score_percent: Optional[float] = None
    created_at: datetime
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    failure_reason: Optional[str] = None
    config_snapshot: Dict[str, Any] = Field(default_factory=dict)
    force_cancel_requested: bool = False

    model_config = {"from_attributes": True}


class TournamentDetailOut(TournamentOut):
    pause_requested: bool
    cancel_requested: bool
    pairs: List[PairJobOut] = Field(default_factory=list)


class HealthOut(BaseModel):
    status: str
    database: str
    worker_heartbeat: str
    cutechess: str
    active_tournament_id: Optional[str] = None
    # Deployment gate: enabled builds whose capability schema is still NULL
    # (migration 0006 backfill pending).  >0 means the worker must not start
    # new tournaments.
    uci_capability_gap: int = 0


# ---------------------------------------------------------------------------
# Public replay (anonymous, read-only; whitelist fields only)
# ---------------------------------------------------------------------------
class PublicGameOut(BaseModel):
    id: str
    game_number: int
    white_engine: str
    black_engine: str
    opening_index: int
    result: Optional[str] = None
    termination: Optional[str] = None
    finished_at: Optional[datetime] = None
    # P4.7: whether a Stockfish analysis artifact exists for this game.
    analyzed: bool = False

    model_config = {"from_attributes": True}


class PublicMatchOut(BaseModel):
    id: str
    name: str
    status: str
    time_control: str
    requested_pairs: int
    completed_pairs: int
    candidate_wins: int
    candidate_losses: int
    draws: int
    score_percent: Optional[float] = None
    # P4.11 commit 4: Engine A vs Engine B performance delta derived from the
    # match score (NOT a P4.8 Arena Elo mutation).  Clamped to +/-800.
    elo_delta: Optional[int] = None
    finished_at: Optional[datetime] = None
    engine_a_label: str
    engine_b_label: str
    opening_set_id: str


class PublicMatchDetailOut(PublicMatchOut):
    games: List[PublicGameOut] = Field(default_factory=list)


class PublicAnalysisPositionOut(BaseModel):
    ply: int
    fen: str
    score_cp: Optional[int] = None
    mate: Optional[int] = None
    best_move: Optional[str] = None
    pv: List[str] = Field(default_factory=list)


class PublicAnalysisOut(BaseModel):
    """Whitelisted per-game Stockfish analysis (P4.7).  Never exposes build
    ids, binary SHAs, server paths or request artifacts."""

    engine_name: str
    limit: dict
    positions: List[PublicAnalysisPositionOut]


class LiveSideOut(BaseModel):
    """One side of the live view (P4.11): label, clock and the engine's own
    latest self-evaluation from the cutechess debug stream."""

    label: str
    clock_ms: Optional[int] = None
    eval_cp: Optional[int] = None
    mate: Optional[int] = None
    depth: Optional[int] = None
    nodes: Optional[int] = None
    nps: Optional[int] = None
    pv: List[str] = Field(default_factory=list)


class LiveOut(BaseModel):
    """Live match status (P4.3 v1 + P4.11 live telemetry).  Only whitelisted
    display fields; never exposes build ids, binary SHAs, paths, logs or
    provenance.

    ``status`` is one of:
      idle       no current match to watch
      live       a match is queued/running; opening/pair/game fields populated
      completed  the watched match finished; ``match_url`` links to the replay
    """

    status: str
    tournament_id: Optional[str] = None
    name: Optional[str] = None
    engine_a_label: Optional[str] = None
    engine_b_label: Optional[str] = None
    time_control: Optional[str] = None
    opening_set_id: Optional[str] = None
    pairs_total: Optional[int] = None
    # Current executing pair (0-based) and game (1-based).
    pair_index: Optional[int] = None
    game_in_pair: Optional[int] = None
    games_total: Optional[int] = None
    state: Optional[str] = None  # pending | game_running | pair_done
    last_result: Optional[str] = None
    opening_fen: Optional[str] = None
    candidate_wins: Optional[int] = None
    candidate_losses: Optional[int] = None
    draws: Optional[int] = None
    match_url: Optional[str] = None
    # P4.12 follow-up: verified pair progress for the public match summary.
    pairs_completed: Optional[int] = None
    # S4.3D SPRT evidence (whitelisted display fields from sprt.json).
    sprt: Optional[dict] = None
    # P4.11 live telemetry (only present when the debug stream is available).
    current_fen: Optional[str] = None
    side_to_move: Optional[str] = None
    last_move: Optional[str] = None
    ply: Optional[int] = None
    telemetry_age_s: Optional[int] = None
    white: Optional[LiveSideOut] = None
    black: Optional[LiveSideOut] = None
