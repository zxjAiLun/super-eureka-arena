"""P4.11 live telemetry tests: parsing the cutechess -debug stream and the
live endpoint exposing the real position / clocks / per-engine self eval."""

from __future__ import annotations

import time
from pathlib import Path

from chessarena.models import RUNNING, Tournament, utcnow
from chessarena.services import live_telemetry

OPENING = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"

DEBUG_LINES = "\n".join(
    [
        "4 >ChessEngine Production(0): uci",
        "6 <ChessEngine Production(0): id name ChessEngineDemo",
        "9 >ChessEngine Production(0): position fen rn1qk2r/ppp1bppp/1n2p3/4Pb2/3P4/1BN5/PP2NPPP/R1BQK2R w KQkq - 2 9",
        "11 >ChessEngine Production(0): go wtime 180000 btime 180000 winc 2000 binc 2000",
        "13 <ChessEngine Production(0): info depth 14 score cp 35 nodes 123456 nps 2000000 time 61 pv e2g3 c7c5 g1e2",
        "14 >Stockfish Limited 2000(0): position fen rn1qk2r/ppp1bppp/1n2p3/4Pb2/3P4/1BN5/PP2NPPP/R1BQK2R w KQkq - 2 9 moves e2g3",
        "15 >Stockfish Limited 2000(0): go wtime 179800 btime 178000 winc 2000 binc 2000",
        "17 <Stockfish Limited 2000(0): info depth 22 score cp -12 nodes 987654 nps 1800000 time 88 pv c7c5 g1e2 d7d5",
        "20 <ChessEngine Production(0): bestmove e2g3 ponder c7c5",
    ]
)


def _write_debug(tmp_path: Path, text: str = DEBUG_LINES) -> Path:
    p = tmp_path / "stdout.log"
    p.write_text(text + "\n", encoding="utf-8")
    return p


def test_parse_live_state_position_and_engines(tmp_path):
    state = live_telemetry.parse_live_state(_write_debug(tmp_path))
    assert state["game"] == 0
    assert state["ply"] == 1
    assert state["last_move"] == "e2g3"
    board = __import__("chess", fromlist=["Board"]).Board(state["current_fen"])
    assert board.turn == __import__("chess", fromlist=["BLACK"]).BLACK
    assert state["side_to_move"] == "b"

    ce = state["engines"]["ChessEngine Production"]
    assert ce["eval_cp"] == 35
    assert ce["depth"] == 14
    assert ce["nodes"] == 123456
    assert ce["pv"] == ["e2g3", "c7c5", "g1e2"]
    sf = state["engines"]["Stockfish Limited 2000"]
    assert sf["eval_cp"] == -12

    # Clocks from the last go each engine received.
    assert state["clocks"]["ChessEngine Production"]["own_ms"] == 180000
    assert state["clocks"]["Stockfish Limited 2000"]["own_ms"] == 179800
    assert state["clocks"]["Stockfish Limited 2000"]["opp_ms"] == 178000


def test_parse_live_state_missing_file(tmp_path):
    state = live_telemetry.parse_live_state(tmp_path / "nope.log")
    assert state["current_fen"] is None
    assert state["engines"] == {}


def test_live_endpoint_telemetry(settings, engine_factory, tournament_factory,
                                 app_client):
    """The live endpoint exposes the real position, colors (game 0 -> A
    white) and per-side self eval; without a debug stream it falls back to
    the opening FEN."""
    tid = tournament_factory(name="live-tel", pairs=1, time_control="blitz_3_2",
                             status=RUNNING)
    with engine_factory() as session:
        t = session.query(Tournament).filter(Tournament.id == tid).one()
        t.started_at = utcnow()
        snap = dict(t.config_snapshot or {})
        snap["engine_a"] = {**snap.get("engine_a", {}),
                            "display_name": "ChessEngine Production"}
        snap["engine_b"] = {**snap.get("engine_b", {}),
                            "display_name": "Stockfish Limited 2000"}
        t.config_snapshot = snap
        pair = t.pair_jobs[0]
        pair.status = RUNNING
        run_dir = settings.run_root / tid / "pairs" / "000000" / "attempt-01"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "opening.epd").write_text(OPENING + "\n", encoding="utf-8")
        (run_dir / "stdout.log").write_text(DEBUG_LINES, encoding="utf-8")
        pair.run_directory = str(run_dir)
        session.commit()

    r = app_client.get(f"/chessarena/public-api/v1/live?tournament_id={tid}")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "live"
    assert body["current_fen"].startswith("rn1qk2r/")
    assert body["side_to_move"] == "b"
    assert body["last_move"] == "e2g3"
    assert body["ply"] == 1
    assert body["telemetry_age_s"] is not None
    # Game 0 -> engine A is White.
    assert body["white"]["label"] == "ChessEngine Production"
    assert body["black"]["label"] == "Stockfish Limited 2000"
    assert body["white"]["eval_cp"] == 35
    assert body["white"]["depth"] == 14
    assert body["black"]["eval_cp"] == -12
    assert body["black"]["clock_ms"] == 179800
    # Whitelist: no internal fields leak.
    text = str(body)
    for forbidden in ("build_id", "binary_sha256", "stdout.log", "run_root"):
        assert forbidden not in text


def test_live_endpoint_falls_back_to_opening_fen(
    settings, engine_factory, tournament_factory, app_client
):
    """Pre-P4.11 matches have no debug stream: still show the opening FEN."""
    tid = tournament_factory(name="live-old", pairs=1, status=RUNNING)
    with engine_factory() as session:
        t = session.query(Tournament).filter(Tournament.id == tid).one()
        t.started_at = utcnow()
        pair = t.pair_jobs[0]
        pair.status = RUNNING
        run_dir = settings.run_root / tid / "pairs" / "000000" / "attempt-01"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "opening.epd").write_text(OPENING + "\n", encoding="utf-8")
        (run_dir / "stdout.log").write_text(
            "Started game 1 of 2 (A vs B)\n", encoding="utf-8"
        )
        pair.run_directory = str(run_dir)
        session.commit()
    r = app_client.get(f"/chessarena/public-api/v1/live?tournament_id={tid}")
    assert r.status_code == 200
    body = r.json()
    assert body["current_fen"] == OPENING
    assert body["white"] is None
