"""P4.11 live telemetry tests: parsing the cutechess -debug stream and the
live endpoint exposing the real position / clocks / per-engine self eval.

Engine identity is the engine INDEX (0 = engine A, 1 = engine B), so
intentional self-play with identical display names cannot merge instances.
A "Started game N" line resets the per-game telemetry."""

from __future__ import annotations

from pathlib import Path

from chessarena.models import PAUSED, PAUSING, RUNNING, Tournament, utcnow
from chessarena.services import live_telemetry

OPENING = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"

# Game 1: engine 0 (ChessEngine, White) opens from a full 6-field FEN with
# castling rights (KQkq) and halfmove/fullmove 0 2; engine 1 (Stockfish, Black)
# receives the position after g1f3 — still with castling rights.
FEN_A = "rnbqkbnr/pp1ppppp/8/2p5/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"
FEN_AFTER = "rnbqkbnr/pp1ppppp/8/2p5/4P3/5N2/PPPP1PPP/RNBQKB1R b KQkq - 1 2"
DEBUG_LINES = "\n".join(
    [
        "Started game 1 of 2 (CurrentFinal LegalityFast vs CurrentFinal)",
        f"4 >ChessEngine Production(0): position fen {FEN_A}",
        "6 >ChessEngine Production(0): go wtime 180000 btime 180000 winc 2000 binc 2000",
        "8 <ChessEngine Production(0): info depth 14 score cp 35 nodes 123456 nps 2000000 time 61 pv g1f3 c7c5 d2d4",
        "10 >Stockfish Limited 2000(1): position fen {} moves g1f3".format(FEN_A),
        "11 >Stockfish Limited 2000(1): go wtime 179900 btime 180000 winc 2000 binc 2000",
        "12 <Stockfish Limited 2000(1): info depth 22 score cp -12 nodes 987654 nps 1800000 time 88 pv c7c5 d2d4 e7e6",
        "14 <ChessEngine Production(0): bestmove g1f3 ponder c7c5",
    ]
)


def _write_debug(tmp_path: Path, text: str = DEBUG_LINES) -> Path:
    p = tmp_path / "stdout.log"
    p.write_text(text + "\n", encoding="utf-8")
    return p


def test_parse_live_state_full_fen_and_engines(tmp_path):
    state = live_telemetry.parse_live_state(_write_debug(tmp_path))
    assert state["game_in_pair"] == 1
    assert state["ply"] == 1
    assert state["last_move"] == "g1f3"
    # The full 6-field FEN is preserved: black to move after g1f3, castling
    # KQkq intact, halfmove/fullmove carried (1 2), not dropped.
    assert state["current_fen"] == FEN_AFTER
    assert state["side_to_move"] == "b"

    ce = state["engines"][0]
    assert ce["eval_cp"] == 35
    assert ce["depth"] == 14
    assert ce["pv"] == ["g1f3", "c7c5", "d2d4"]
    sf = state["engines"][1]
    assert sf["eval_cp"] == -12

    # Latest go carries BOTH absolute clocks; active engine = the side to move.
    assert state["go"] == {"wtime": 179900, "btime": 180000}
    assert state["active_engine"] == 1


def test_parse_live_state_same_name_does_not_overwrite(tmp_path):
    """Engine identity is the index: SameEngine(0) and SameEngine(1) must not
    overwrite each other."""
    text = "\n".join(
        [
            "Started game 1 of 2 (SameEngine vs SameEngine)",
            f"4 >SameEngine(0): position fen {OPENING}",
            "6 >SameEngine(0): go wtime 180000 btime 180000 winc 2000 binc 2000",
            "8 <SameEngine(0): info depth 10 score cp 50 nodes 100 nps 1000 time 5 pv e2e4",
            "10 >SameEngine(1): position fen {} moves e2e4".format(OPENING),
            "11 >SameEngine(1): go wtime 179000 btime 180000 winc 2000 binc 2000",
            "12 <SameEngine(1): info depth 20 score cp -30 nodes 200 nps 2000 time 9 pv e7e5",
        ]
    )
    state = live_telemetry.parse_live_state(_write_debug(tmp_path, text))
    assert state["engines"][0]["eval_cp"] == 50
    assert state["engines"][1]["eval_cp"] == -30
    assert state["active_engine"] == 1


def test_parse_live_state_game_boundary_resets(tmp_path):
    """Started game 2 clears game-1 telemetry (evals, clocks, last move)."""
    text = "\n".join(
        [
            "Started game 1 of 2 (A vs B)",
            f"4 >A(0): position fen {OPENING}",
            "6 >A(0): go wtime 180000 btime 180000 winc 2000 binc 2000",
            "8 <A(0): info depth 10 score cp 99 nodes 100 nps 1000 time 5 pv d2d4",
            "10 <A(0): bestmove d2d4 ponder d7d5",
            "Started game 2 of 2 (B vs A)",
            "20 >B(1): position fen {} moves e2e4".format(OPENING),
            "22 >B(1): go wtime 179500 btime 180000 winc 2000 binc 2000",
        ]
    )
    state = live_telemetry.parse_live_state(_write_debug(tmp_path, text))
    assert state["game_in_pair"] == 2
    # Game-1's last move (d2d4) is gone; game 2's e2e4 is the current move.
    assert state["last_move"] == "e2e4"
    assert state["engines"][0] == {}  # game-1 eval cleared
    assert state["engines"][1] == {}
    assert state["go"]["wtime"] == 179500


def test_parse_live_state_missing_file(tmp_path):
    state = live_telemetry.parse_live_state(tmp_path / "nope.log")
    assert state["current_fen"] is None
    assert state["engines"] == {0: {}, 1: {}}


def test_live_endpoint_telemetry(settings, engine_factory, tournament_factory,
                                 app_client):
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
    # Full FEN preserved (black to move after g1f3).
    board = __import__("chess", fromlist=["Board"]).Board(body["current_fen"])
    assert board.turn == __import__("chess", fromlist=["BLACK"]).BLACK
    assert body["side_to_move"] == "b"
    assert body["last_move"] == "g1f3"
    assert body["ply"] == 1
    # Game 1 -> engine A white; engine index maps 0 -> A, 1 -> B.
    assert body["white"]["label"] == "ChessEngine Production"
    assert body["black"]["label"] == "Stockfish Limited 2000"
    assert body["white"]["eval_cp"] == 35
    assert body["black"]["eval_cp"] == -12
    # Clocks: white frozen at latest wtime; black (active) minus its info time.
    assert body["white"]["clock_ms"] == 179900
    assert body["black"]["clock_ms"] == 180000 - 88
    # Whitelist: no internal fields leak.
    text = str(body)
    for forbidden in ("build_id", "binary_sha256", "stdout.log", "run_root"):
        assert forbidden not in text


def test_live_endpoint_falls_back_to_opening_fen(
    settings, engine_factory, tournament_factory, app_client
):
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


def test_live_auto_detect_excludes_paused(settings, engine_factory,
                                          tournament_factory, app_client):
    tid = tournament_factory(name="paused", pairs=1, status=PAUSED)
    with engine_factory() as session:
        t = session.query(Tournament).filter(Tournament.id == tid).one()
        t.started_at = utcnow()
        session.commit()
    r = app_client.get("/chessarena/public-api/v1/live")
    assert r.json()["status"] == "idle"


def test_live_auto_detect_includes_pausing(settings, engine_factory,
                                           tournament_factory, app_client):
    tid = tournament_factory(name="pausing", pairs=1, status=PAUSING)
    with engine_factory() as session:
        t = session.query(Tournament).filter(Tournament.id == tid).one()
        t.started_at = utcnow()
        session.commit()
    r = app_client.get("/chessarena/public-api/v1/live")
    assert r.json()["status"] == "live"
    assert r.json()["tournament_id"] == tid
