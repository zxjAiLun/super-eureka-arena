"""Runtime configuration loaded from environment variables.

All settings are read from the environment (``/etc/chessarena/chessarena.env``
on the server) so the API process, the worker process and management scripts
share one source of truth.  Every setting has a local default so the test
suite can run without any server-side files.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict


# ---------------------------------------------------------------------------
# Fixed time-control presets (section 6 of the spec).
# Only these values may be passed to cutechess; arbitrary TC strings are never
# accepted from the API.
# ---------------------------------------------------------------------------
TIME_CONTROLS: Dict[str, Dict[str, str]] = {
    "bullet_1_0": {
        "label": "Bullet 1+0",
        "friendly_label": "1+0",
        "cutechess_tc": "60",
    },
    "blitz_3_2": {
        "label": "Blitz 3+2",
        "friendly_label": "3+2",
        "cutechess_tc": "180+2",
    },
    "blitz_10_01": {
        "label": "10s+0.1s",
        "friendly_label": "10s+0.1s",
        "cutechess_tc": "10+0.1",
    },
    "rapid_5_3": {
        "label": "Rapid 5+3",
        "friendly_label": "5+3",
        "cutechess_tc": "300+3",
    },
}

# Player name labels used on the cutechess command line and in PGN headers.
# Engine A is the "candidate"; Engine B is the "baseline".
ENGINE_A_NAME = "EngineA"
ENGINE_B_NAME = "EngineB"


@dataclass(frozen=True)
class Settings:
    # Database
    db_url: str = os.environ.get(
        "ARENA_DB_URL", "sqlite:////var/lib/chessarena/arena.db"
    )

    # Directory layout
    run_root: Path = Path(
        os.environ.get("ARENA_RUN_ROOT", "/var/lib/chessarena/runs")
    )
    build_root: Path = Path(
        os.environ.get("ARENA_BUILD_ROOT", "/opt/chessarena/builds")
    )
    opening_root: Path = Path(
        os.environ.get("ARENA_OPENING_ROOT", "/opt/chessarena/openings")
    )

    # Cutechess binary
    cutechess: Path = Path(
        os.environ.get("ARENA_CUTECHESS", "/usr/bin/cutechess-cli")
    )

    # Fixed production constraints (section 2.3) - not user configurable via API
    max_concurrency: int = int(os.environ.get("ARENA_MAX_CONCURRENCY", "1"))
    hash_mb: int = int(os.environ.get("ARENA_HASH_MB", "32"))
    threads: int = int(os.environ.get("ARENA_THREADS", "1"))

    # URL layout behind the reverse proxy
    base_path: str = os.environ.get("ARENA_BASE_PATH", "/chessarena")
    public_url: str = os.environ.get(
        "ARENA_PUBLIC_URL", "https://pearllover.site/chessarena"
    )

    # Worker behavior
    worker_poll_seconds: float = float(
        os.environ.get("ARENA_WORKER_POLL_SECONDS", "2.0")
    )
    worker_heartbeat_seconds: float = float(
        os.environ.get("ARENA_WORKER_HEARTBEAT_SECONDS", "2.0")
    )
    # A heartbeat older than this makes /health report the worker as stale.
    worker_stale_seconds: float = float(
        os.environ.get("ARENA_WORKER_STALE_SECONDS", "15.0")
    )
    # How long the worker waits for cutechess to exit after SIGTERM before
    # sending SIGKILL (section 19).
    shutdown_grace_seconds: float = float(
        os.environ.get("ARENA_SHUTDOWN_GRACE_SECONDS", "15.0")
    )

    # Logging
    log_level: str = os.environ.get("ARENA_LOG_LEVEL", "INFO")

    # ------------------------------------------------------------------
    # Human vs Engine play (dark-launch feature; see docs/design ADR on
    # engine-version identity for why opponents are frozen at game start).
    # ------------------------------------------------------------------
    # Master switch.  OFF by default: every human-play page and API route
    # fails closed with 404 until it is explicitly enabled in the env file.
    human_play_enabled: bool = os.environ.get(
        "ARENA_HUMAN_PLAY_ENABLED", "false"
    ).lower() in ("1", "true", "yes", "on")
    # Explicit opponent allowlist — comma-separated refs of the form
    # "preset:<preset_id>" or "channel:<channel_id>".  Public visibility or a
    # preset category NEVER grants human-play rights on its own.
    human_play_opponents: str = os.environ.get(
        "ARENA_HUMAN_PLAY_OPPONENTS", ""
    )
    # Fixed engine move budget (server-side hard cap; clients never send it).
    human_play_movetime_ms: int = int(
        os.environ.get("ARENA_HUMAN_PLAY_MOVETIME_MS", "1000")
    )
    # Per-IP creation limits (abuse guardrails for an anonymous surface).
    human_play_max_active_per_ip: int = int(
        os.environ.get("ARENA_HUMAN_PLAY_MAX_ACTIVE_PER_IP", "2")
    )
    human_play_max_created_per_hour: int = int(
        os.environ.get("ARENA_HUMAN_PLAY_MAX_CREATED_PER_HOUR", "5")
    )
    human_play_max_total_active: int = int(
        os.environ.get("ARENA_HUMAN_PLAY_MAX_TOTAL_ACTIVE", "8")
    )
    # Absolute game lifetime / idle expiry (seconds).
    human_play_ttl_seconds: int = int(
        os.environ.get("ARENA_HUMAN_PLAY_TTL_SECONDS", str(24 * 3600))
    )
    human_play_idle_seconds: int = int(
        os.environ.get("ARENA_HUMAN_PLAY_IDLE_SECONDS", str(3600))
    )
    # How long the browser may keep polling one game's state.
    human_play_poll_seconds: float = float(
        os.environ.get("ARENA_HUMAN_PLAY_POLL_SECONDS", "0.4")
    )

    # Stderr whitelist: substrings that are acceptable in a cutechess child's
    # stderr during a pair run.  Anything else fails verification.
    stderr_whitelist: tuple = field(
        default=(
            "info string",
            "Searching:",
            "Time control:",
            "Playing standard chess",
            "game",  # e.g. "Finished game 1"
        )
    )

    @property
    def api_host(self) -> str:
        return "127.0.0.1"

    @property
    def api_port(self) -> int:
        return int(os.environ.get("ARENA_API_PORT", "8787"))

    def human_play_opponent_refs(self) -> tuple[str, ...]:
        """Parsed allowlist refs: ``("preset:...", "channel:...", ...)``."""
        refs = []
        for chunk in self.human_play_opponents.split(","):
            ref = chunk.strip()
            if ref:
                refs.append(ref)
        return tuple(refs)

    def __post_init__(self) -> None:
        # P2-2: the engine movetime is a server-side HARD CAP, not merely a
        # default — an operator typo (e.g. 600000) must fail fast at startup
        # instead of silently letting anonymous play pin the CPU for minutes
        # per move.  Range matches the frozen MVP contract.
        movetime = self.human_play_movetime_ms
        if not (100 <= movetime <= 3000):
            raise ValueError(
                "ARENA_HUMAN_PLAY_MOVETIME_MS must be between 100 and 3000 "
                f"ms (hard cap), got {movetime}"
            )


def get_settings() -> Settings:
    return Settings()
