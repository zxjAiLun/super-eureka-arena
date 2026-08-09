"""P4.7 post-game Stockfish analysis tests: request creation, rejection rules,
the analyzer pipeline (ply/FEN/score/PV alignment + white perspective), and
the public analysis API."""

from __future__ import annotations

import sys
from pathlib import Path

import chess
import chess.pgn
import pytest

from chessarena.models import COMPLETED, DRAFT, EngineBuild, Game, Tournament
from chessarena.services import analysis

FIXTURES = Path(__file__).resolve().parent / "fixtures"

# The analyzer must be launchable on this platform: use the .cmd shim on
# Windows (like the fake cutechess fixture) and the script elsewhere.
FAKE_ENGINE = (
    FIXTURES / "fake_uci_engine.cmd"
    if sys.platform == "win32"
    else FIXTURES / "fake_uci_engine.py"
)

ANALYSIS_PGN = "\n".join(
    [
        '[Event "?"]',
        '[Site "?"]',
        '[Date "2026.08.09"]',
        '[Round "1"]',
        '[White "EngineA"]',
        '[Black "EngineB"]',
        '[Result "1-0"]',
        "",
        "1. e4 e5 2. Nf3 Nc6 1-0",
        "",
    ]
)


def _write_game_pgn(settings, tid):
    pgn_path = settings.run_root / tid / "pairs" / "000000" / "attempt-01" / "match.pgn"
    pgn_path.parent.mkdir(parents=True, exist_ok=True)
    pgn_path.write_text(ANALYSIS_PGN, encoding="utf-8")
    return pgn_path


def _completed_game(settings, engine_factory, tournament_factory, name="Analysis"):
    tid = tournament_factory(name=name, pairs=1, status=COMPLETED)
    with engine_factory() as session:
        t = session.query(Tournament).filter(Tournament.id == tid).one()
        pair = t.pair_jobs[0]
        pgn_path = _write_game_pgn(settings, tid)
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


def test_request_analysis_writes_artifact(settings, engine_factory,
                                          tournament_factory):
    tid, gid = _completed_game(settings, engine_factory, tournament_factory)
    with engine_factory() as session:
        game = session.query(Game).filter(Game.id == gid).one()
        analysis.request_analysis(settings, game)
    assert analysis.request_path(tid, gid).is_file()
    assert analysis.analysis_state(game) == "queued"
    # Re-request after a result clears it (re-analyze).
    out = analysis.result_path(tid, gid)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("{}", encoding="utf-8")
    with engine_factory() as session:
        game = session.query(Game).filter(Game.id == gid).one()
        assert analysis.analysis_state(game) == "ready"
        analysis.request_analysis(settings, game)
        assert analysis.analysis_state(game) == "queued"
        assert not out.exists()


def test_unverified_or_incomplete_rejected(settings, engine_factory,
                                           tournament_factory, app_client):
    tid, gid = _completed_game(settings, engine_factory, tournament_factory)
    # Admin analyze on a non-verified game -> 409.
    with engine_factory() as session:
        game = session.query(Game).filter(Game.id == gid).one()
        game.verified = False
        session.commit()
    app_client.get("/chessarena/admin/tournaments/new")
    token = app_client.cookies.get("arena_csrf")
    r = app_client.post(
        f"/chessarena/admin/games/{gid}/analyze",
        data={"_csrf_token": token},
        follow_redirects=False,
    )
    assert r.status_code == 409
    # A verified game inside a DRAFT tournament -> 409.
    with engine_factory() as session:
        game = session.query(Game).filter(Game.id == gid).one()
        game.verified = True
        t = session.query(Tournament).filter(Tournament.id == tid).one()
        t.status = DRAFT
        session.commit()
    r = app_client.post(
        f"/chessarena/admin/games/{gid}/analyze",
        data={"_csrf_token": token},
        follow_redirects=False,
    )
    assert r.status_code == 409


def _use_fake_analyzer(engine_factory):
    """Point the registered build at the fake UCI engine as the analyzer."""
    with engine_factory() as session:
        build = session.query(EngineBuild).first()
        build.engine_name = "Stockfish"
        build.binary_path = str(FAKE_ENGINE)
        session.commit()


def test_analyzer_pipeline_alignment(settings, engine_factory,
                                     tournament_factory):
    """Run the real analyzer against the fake UCI engine: one position per
    ply, FENs in order, scores in White perspective, best move + PV set."""
    tid, gid = _completed_game(settings, engine_factory, tournament_factory)
    _use_fake_analyzer(engine_factory)
    with engine_factory() as session:
        game = session.query(Game).filter(Game.id == gid).one()
        analysis.request_analysis(settings, game)
        outcome = analysis.run_analysis(settings, session, game)
        if outcome == "failed":
            err = analysis.error_path(tid, gid)
            print("ERR_ARTIFACT=" + (err.read_text() if err.exists() else "none"))
        assert outcome == "completed"
        data = analysis.read_analysis(game)
    assert data is not None
    assert data["game_id"] == gid
    assert data["limit"] == {"type": "nodes", "value": analysis.ANALYSIS_NODES}
    expected = [
        "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
    ]
    board = chess.Board()
    for m in ["e2e4", "e7e5", "g1f3", "b8c6"]:
        board.push_uci(m)
        expected.append(board.fen())
    assert len(data["positions"]) == len(expected)
    for i, (pos, fen) in enumerate(zip(data["positions"], expected)):
        assert pos["ply"] == i
        assert pos["fen"] == fen
        # Fake engine reports cp 18 from the side to move: white at even plies.
        assert pos["score_cp"] == (18 if i % 2 == 0 else -18)
        assert pos["best_move"] == "e2e4"
        assert pos["pv"][0] == "e2e4"


def test_analysis_api_404_when_unanalyzed(app_client, settings, engine_factory,
                                          tournament_factory):
    tid, gid = _completed_game(settings, engine_factory, tournament_factory)
    r = app_client.get(f"/chessarena/public-api/v1/games/{gid}/analysis")
    assert r.status_code == 404


def test_analysis_api_reads_artifact(app_client, settings, engine_factory,
                                     tournament_factory):
    tid, gid = _completed_game(settings, engine_factory, tournament_factory)
    _use_fake_analyzer(engine_factory)
    with engine_factory() as session:
        game = session.query(Game).filter(Game.id == gid).one()
        analysis.request_analysis(settings, game)
        analysis.run_analysis(settings, session, game)
    r = app_client.get(f"/chessarena/public-api/v1/games/{gid}/analysis")
    assert r.status_code == 200
    body = r.json()
    assert body["engine_name"] == "Stockfish"
    assert body["limit"]["type"] == "nodes"
    assert len(body["positions"]) == 5
    # Whitelist: no internal fields leak.
    text = str(body)
    for forbidden in ("build_id", "binary_sha256", "pgn_path", "run_root"):
        assert forbidden not in text
