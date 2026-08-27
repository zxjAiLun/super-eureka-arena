"""Worker entry point (section 19).

The worker owns the scheduler.  It:
1. runs recovery once at boot,
2. loops: scheduler tick + worker heartbeat,
3. on SIGTERM/SIGINT stops scheduling, terminates the cutechess process group
   (15s grace then SIGKILL), and marks the current attempt INTERRUPTED.

Run as:  python -m chessarena.worker
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
import time
from datetime import datetime, timezone

from .config import Settings, get_settings
from .db import make_engine, make_session_factory
from .models import Game, WorkerState
from .services import analysis, artifacts, recovery
from .services.scheduler import Scheduler

logger = logging.getLogger("chessarena.worker")

_stop_event = threading.Event()


def _start_analysis_if_queued(
    settings: Settings, session_factory
) -> threading.Thread | None:
    """When the match queue is idle, run one queued game analysis in a daemon
    thread (the current game finishes even if a match arrives afterwards)."""
    with session_factory() as session:
        game = analysis.next_request(session, settings)
        if game is None:
            return None

    def _run() -> None:
        try:
            with session_factory() as session:
                outcome = analysis.run_analysis(settings, session, game)
            logger.info("analysis: game %s %s", game.id, outcome)
        except Exception:
            logger.exception("analysis failed for game %s", game.id)

    thread = threading.Thread(target=_run, name="analysis", daemon=True)
    thread.start()
    logger.info("analysis: started game %s", game.id)
    return thread


def _service_human_move(settings: Settings, session_factory) -> bool:
    """Execute at most one pending human-play engine move.

    Called between scheduler ticks ONLY when no cutechess pair is running.
    The scheduler is then re-polled immediately afterwards so a queued match
    still starts on the very next tick — a human move merely inserts a
    1-2 second gap between pairs, it never computes concurrently with a
    timed match (CPU isolation contract).
    """
    from .models import HumanGame
    from .services import human_engine

    try:
        with session_factory() as session:
            game = human_engine.next_pending_game(session)
            if game is None:
                return False
            game_id = game.id
        with session_factory() as session:
            game = session.get(HumanGame, game_id)
            if game is None:
                return False
            action = human_engine.service_pending_move(settings, session, game)
        if action and not action.startswith("human-move skipped"):
            logger.info("human-play: %s", action)
        return True
    except Exception:
        logger.exception("human-play move servicing failed")
        return False


def _worker_step(settings, session_factory, scheduler, analysis_thread):
    """One iteration of match/human-play/analysis arbitration.

    Contract: the RUNNING pair always finishes first; between pairs, at most
    ONE pending human-play engine move is serviced before the next queued
    pair launches (a 1-2 second gap, never concurrent computation); the
    scheduler is then re-polled immediately so a queued match starts on the
    very next tick.  Post-game analysis stays last-priority.  No two
    CPU-heavy workloads ever overlap — the experiment-correctness invariant
    of the Arena.
    """
    if analysis_thread is not None and analysis_thread.is_alive():
        # A game analysis is in flight: matches wait for it to finish.  This
        # keeps the analyzer (Threads=2, 256 MB) from sharing CPU with a
        # timed match, which would pollute measured strength.
        return "analysis-running", analysis_thread
    if getattr(scheduler, "active_proc", None) is not None:
        # A timed pair is running: it wins unconditionally.  Human-play
        # moves keep waiting (the browser keeps polling).
        action = scheduler.tick()
        return action, analysis_thread
    # Between pairs: service one pending human move BEFORE launching the
    # next pair (bounded delay, zero concurrent computation).  Skipped when
    # the feature is disabled or settings are unavailable (test doubles).
    if (
        settings is not None
        and session_factory is not None
        and settings.human_play_enabled
        and _service_human_move(settings, session_factory)
    ):
        return "human-move", analysis_thread
    action = scheduler.tick()
    if action != "idle":
        return action, analysis_thread
    analysis_thread = _start_analysis_if_queued(settings, session_factory)
    if analysis_thread is not None:
        return "analysis-started", analysis_thread
    return "idle", analysis_thread


def _handle_signal(signum, frame):
    logger.info("received signal %s, shutting down", signum)
    _stop_event.set()


def _heartbeat(session_factory, scheduler: Scheduler) -> None:
    with session_factory() as session:
        state = session.get(WorkerState, 1)
        if state is None:
            state = WorkerState(id=1)
            session.add(state)
        now = datetime.now(timezone.utc)
        state.status = "running" if scheduler.active_proc else "idle"
        state.heartbeat_at = now
        state.pid = scheduler.active_proc.pid if scheduler.active_proc else None
        state.tournament_id = scheduler.active_tournament_id
        state.pair_job_id = scheduler.active_pair_job_id
        if scheduler.active_proc is None:
            # Clear the recorded process identity once the pair is no longer
            # supervised so recovery never chases a stale PID.
            state.pid_start_marker = None
            state.pid_cmdline = None
        session.commit()


def run_worker(settings: Settings, session_factory) -> int:
    # Deployment gate (P4.F1 B3b): refuse to start new tournaments while any
    # enabled build lacks a probed capability schema — running matches would
    # silently omit runtime options the frozen snapshot expects.
    with session_factory() as session:
        from .services.capabilities import enabled_builds_without_uci_schema

        gap = enabled_builds_without_uci_schema(session)
    if gap > 0:
        logger.error(
            "refusing to start: %d enabled build(s) have NULL "
            "uci_options_schema; run scripts/probe_build_capabilities.py "
            "to backfill before starting matches",
            gap,
        )
        return 1

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    logger.info("worker starting; running recovery")
    recovery.run_recovery(settings, session_factory)

    scheduler = Scheduler(settings, session_factory)
    logger.info("worker recovery complete; entering scheduler loop")

    analysis_thread: threading.Thread | None = None
    last_beat = 0.0
    while not _stop_event.is_set():
        started = time.monotonic()
        try:
            action, analysis_thread = _worker_step(
                settings, session_factory, scheduler, analysis_thread
            )
            if action not in ("idle", "analysis-running"):
                logger.info("tick: %s", action)
        except Exception:
            logger.exception("scheduler tick failed")
        if time.monotonic() - last_beat >= settings.worker_heartbeat_seconds:
            try:
                _heartbeat(session_factory, scheduler)
                last_beat = time.monotonic()
            except Exception:
                logger.exception("heartbeat failed")

        elapsed = time.monotonic() - started
        sleep_for = max(0.0, settings.worker_poll_seconds - elapsed)
        if not _stop_event.wait(sleep_for):
            continue
        break

    logger.info("worker stopping; terminating active process group if any")
    scheduler.shutdown()
    _mark_stopped(session_factory)
    logger.info("worker stopped cleanly")
    return 0


def _mark_stopped(session_factory) -> None:
    try:
        with session_factory() as session:
            state = session.get(WorkerState, 1)
            if state is None:
                state = WorkerState(id=1)
                session.add(state)
            state.status = "stopped"
            state.heartbeat_at = datetime.now(timezone.utc)
            session.commit()
    except Exception:
        logger.exception("failed to mark worker stopped")


def main() -> int:
    logging.basicConfig(
        level=logging.getLevelName(get_settings().log_level),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = get_settings()
    artifacts.configure_artifact_service(settings)
    engine = make_engine(settings.db_url)
    session_factory = make_session_factory(engine)
    return run_worker(settings, session_factory)


if __name__ == "__main__":
    sys.exit(main())
