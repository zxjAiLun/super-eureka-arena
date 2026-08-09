"""P4.7 repair regressions: match/analysis arbitration in the worker, analyzer
search timeouts, and delete-vs-analysis abandonment."""

from __future__ import annotations

import shutil
import sys
import threading
import time
from pathlib import Path

import chess
import pytest

from chessarena.models import COMPLETED, EngineBuild, Game, Tournament
from chessarena.services import analysis
from chessarena.worker import _worker_step

FIXTURES = Path(__file__).resolve().parent / "fixtures"

HANG_ENGINE = (
    FIXTURES / "fake_uci_hang.cmd"
    if sys.platform == "win32"
    else FIXTURES / "fake_uci_hang.py"
)

PGN = "\n".join(
    [
        '[Event "?"]',
        '[Site "?"]',
        '[Date "2026.08.09"]',
        '[Round "1"]',
        '[White "EngineA"]',
        '[Black "EngineB"]',
        '[Result "1-0"]',
        "",
        "1. e4 e5 1-0",
        "",
    ]
)


def _completed_game(settings, engine_factory, tournament_factory):
    tid = tournament_factory(name="repair", pairs=1, status=COMPLETED)
    with engine_factory() as session:
        t = session.query(Tournament).filter(Tournament.id == tid).one()
        pair = t.pair_jobs[0]
        pgn_path = settings.run_root / tid / "pairs" / "000000" / "attempt-01" / "match.pgn"
        pgn_path.parent.mkdir(parents=True, exist_ok=True)
        pgn_path.write_text(PGN, encoding="utf-8")
        pair.status = "COMPLETED"
        pair.run_directory = str(pgn_path.parent)
        g = Game(
            tournament_id=tid,
            pair_job_id=pair.id,
            game_number=1,
            white_engine="EngineA",
            black_engine="EngineB",
            opening_index=0,
            result="1-0",
            pgn_path=str(pgn_path),
            verified=True,
        )
        session.add(g)
        session.commit()
        session.refresh(g)
        return tid, g.id


def _set_analyzer(engine_factory, binary):
    with engine_factory() as session:
        build = session.query(EngineBuild).first()
        build.engine_name = "Stockfish"
        build.binary_path = str(binary)
        session.commit()


class _FakeScheduler:
    def __init__(self):
        self.ticks = 0
        self.result = "idle"

    def tick(self):
        self.ticks += 1
        return self.result


def test_worker_step_never_ticks_while_analysis_alive():
    """P1 repair: while an analysis thread is alive the scheduler must not be
    ticked (that would launch a new CuteChess pair and compete for CPU)."""
    scheduler = _FakeScheduler()
    alive = threading.Thread(target=time.sleep, args=(0.4,))
    alive.start()
    action, _ = _worker_step(None, None, scheduler, alive)
    assert action == "analysis-running"
    assert scheduler.ticks == 0
    alive.join()

    # Once finished, the scheduler tick is allowed again (match priority).
    scheduler.result = "launched"
    action, _ = _worker_step(None, None, scheduler, alive)
    assert action == "launched"
    assert scheduler.ticks == 1


def test_analysis_timeout_writes_error(settings, engine_factory,
                                       tournament_factory, monkeypatch):
    """A hung analyzer must time out (the monkeypatched 1s deadline is real),
    get terminated, and produce .error.json instead of blocking forever."""
    monkeypatch.setattr(analysis, "SEARCH_TIMEOUT", 1.0)
    tid, gid = _completed_game(settings, engine_factory, tournament_factory)
    _set_analyzer(engine_factory, HANG_ENGINE)
    started = time.monotonic()
    with engine_factory() as session:
        game = session.query(Game).filter(Game.id == gid).one()
        analysis.request_analysis(settings, game)
        outcome = analysis.run_analysis(settings, session, game)
        assert outcome == "failed"
        err = analysis.error_path(tid, gid)
        assert err.is_file()
        assert analysis.analysis_state(game) == "failed"
    # SEARCH_TIMEOUT is read at call time, so the 1s deadline really applied.
    assert time.monotonic() - started < 10, "timeout was not honored"


def test_analysis_abandoned_when_match_deleted(settings, engine_factory,
                                               tournament_factory):
    """Deleting a match while its analysis is in flight must not recreate
    orphan artifacts under the removed run dir."""
    tid, gid = _completed_game(settings, engine_factory, tournament_factory)
    with engine_factory() as session:
        game = session.query(Game).filter(Game.id == gid).one()
        analysis.request_analysis(settings, game)
    # The admin delete removes DB rows and the whole run dir.
    with engine_factory() as session:
        from chessarena.models import Event, PairJob

        session.query(Game).filter(Game.tournament_id == tid).delete()
        session.query(Event).filter(Event.tournament_id == tid).delete()
        session.query(PairJob).filter(PairJob.tournament_id == tid).delete()
        session.query(Tournament).filter(Tournament.id == tid).delete()
        session.commit()
    shutil.rmtree(settings.run_root / tid, ignore_errors=True)
    assert not (settings.run_root / tid).exists()

    with engine_factory() as session:
        outcome = analysis.run_analysis(settings, session, game)
        assert outcome == "abandoned"
    # Nothing was recreated under the deleted match's run dir.
    assert not (settings.run_root / tid).exists()


def test_analysis_abandoned_on_delete_during_write(
    settings, engine_factory, tournament_factory, monkeypatch
):
    """TOCTOU window: the match vanishes between the existence check and the
    artifact write.  The writer never mkdirs, so the run dir must not come
    back to life."""
    tid, gid = _completed_game(settings, engine_factory, tournament_factory)
    with engine_factory() as session:
        game = session.query(Game).filter(Game.id == gid).one()
        analysis.request_analysis(settings, game)
    # Force the existence check to pass (simulating the check-before-delete
    # interleaving) while the run dir is already gone.
    monkeypatch.setattr(analysis, "_match_still_exists", lambda s, g: True)
    shutil.rmtree(settings.run_root / tid, ignore_errors=True)
    assert not (settings.run_root / tid).exists()
    with engine_factory() as session:
        outcome = analysis.run_analysis(settings, session, game)
        assert outcome == "abandoned"
    assert not (settings.run_root / tid).exists()
