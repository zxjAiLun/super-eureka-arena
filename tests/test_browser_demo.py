"""Browser E2E for the modern React replay demo (P4.UI-1).

Starts a real uvicorn server, opens /chessarena/games/{id} in Chromium
and asserts the React island actually works: board mounted, initial
position correct, PGN move count, clicking a ply sets the right FEN,
first/prev/next/last, keyboard navigation, metadata badges, mobile
responsive layout, and zero console/page errors.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

import chess
import pytest

pytest.importorskip("playwright")

from chessarena.models import COMPLETED, Game, Tournament  # noqa: E402

ARENA_ROOT = Path(__file__).resolve().parents[1]

# Contains captures (2.exd5, 2...Qxd5) to exercise move application from
# verbose from/to squares (a regression where SAN re-parse failed on real
# tournament PGNs with "Invalid move: Bxc6").
SAMPLE_PGN = "\n".join(
    [
        '[Event "E2E"]',
        '[Site "?"]',
        '[Date "2026.08.07"]',
        '[Round "1"]',
        '[White "EngineA"]',
        '[Black "EngineB"]',
        '[Result "1-0"]',
        "",
        "1. e4 d5 2. exd5 Qxd5 3. Nc3 Qd8 1-0",
        "",
    ]
)

START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
PLY3_FEN = "rnbqkbnr/ppp1pppp/8/3P4/8/8/PPPP1PPP/RNBQKBNR b KQkq - 0 2"
PLY4_FEN = "rnb1kbnr/ppp1pppp/8/3q4/8/8/PPPP1PPP/RNBQKBNR w KQkq - 0 3"
PLY5_FEN = "rnb1kbnr/ppp1pppp/8/3q4/8/2N5/PPPP1PPP/R1BQKBNR b KQkq - 1 3"
PLY6_FEN = "rnbqkbnr/ppp1pppp/8/8/8/2N5/PPPP1PPP/R1BQKBNR w KQkq - 2 4"


FEN_HEADER_PGN = "\n".join(
    [
        '[Event "E2E"]',
        '[Site "?"]',
        '[Date "2026.08.07"]',
        '[Round "3"]',
        '[White "EngineA"]',
        '[Black "EngineB"]',
        '[Result "*"]',
        '[FEN "r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3"]',
        "",
        "3. Bb5 a6 4. Ba4 Nf6 5. O-O Be7 *",
        "",
    ]
)
FEN_START = "r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3"
FEN_PLY2 = "r1bqkbnr/1ppp1ppp/p1n5/1B2p3/4P3/5N2/PPPP1PPP/RNBQK2R w KQkq - 0 4"

# Deterministic 80-ply legal game (LCG seed 42) long enough to scroll the
# move list; used to verify the active move auto-scrolls into view.
LONG_PGN = "\n".join(
    [
        '[Event "E2E"]',
        '[Site "?"]',
        '[Date "2026.08.07"]',
        '[Round "1"]',
        '[White "EngineA"]',
        '[Black "EngineB"]',
        '[Result "*"]',
        "",
        "1. f4 d6 2. Nf3 e6 3. h3 c6 4. c4 Qf6 5. f5 Nh6 6. Ng1 Nxf5 7. Qc2 Qg5 "
        "8. Qd3 a5 9. Qd5 Qg3+ 10. Kd1 Qf2 11. h4 b5 12. Kc2 Nd7 13. g3 Qe1 "
        "14. Nc3 Ba6 15. cxb5 a4 16. bxc6 Nc5 17. Nd1 Ne3+ 18. Kb1 Nb7 19. c7 e5 "
        "20. h5 Nxd5 21. Nf2 Qxd2 22. Nf3 Na5 23. Bg2 Nc6 24. Rf1 Rg8 25. Bxd2 g6 "
        "26. c8=N Bxe2 27. Nxd6+ Ke7 28. Re1 Rg7 29. Nh2 Nc7 30. Bh1 Nd8 "
        "31. Rd1 h6 32. Nf3 Ra7 33. Nxe5 Nb7 34. Bf3 Bxd1 35. Nexf7 a3 "
        "36. Nde4 Nb5 37. Nxh6 Ra8 38. hxg6 Na5 39. b4 Bxf3 40. g4 Rh7",
        "",
    ]
)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_until_up(url: str, timeout: float = 40.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=2)
            return
        except Exception:
            time.sleep(0.5)
    raise AssertionError(f"server did not come up at {url}")


def test_browser_demo_replay(settings, engine_factory, registered):
    import json

    manifest = json.loads(
        (registered["build_dir"] / "manifest.json").read_text(encoding="utf-8")
    )
    opening_manifest = json.loads(
        (registered["opening_dir"] / "manifest.json").read_text(encoding="utf-8")
    )

    with engine_factory() as session:
        tournament = Tournament(
            id=str(uuid.uuid4()),
            name="e2e-demo",
            status=COMPLETED,
            engine_a_build_id=manifest["build_id"],
            engine_a_profile="current-final",
            engine_b_build_id=manifest["build_id"],
            engine_b_profile="current",
            opening_set_id=opening_manifest["opening_set_id"],
            time_control="blitz_3_2",
            requested_pairs=2,
            completed_pairs=2,
            config_snapshot={
                "engine_a": {
                    "build_id": manifest["build_id"],
                    "profile": "current-final",
                    "command_args": ["--profile", "current-final"],
                    "uci_options": {},
                },
                "engine_b": {
                    "build_id": manifest["build_id"],
                    "profile": "current",
                    "command_args": ["--profile", "current"],
                    "uci_options": {},
                },
                "time_control": "blitz_3_2",
                "hash_mb": 32,
                "threads": 1,
            },
        )
        session.add(tournament)
        session.flush()
        from chessarena.models import PairJob

        pair = PairJob(
            id=str(uuid.uuid4()),
            tournament_id=tournament.id,
            pair_index=0,
            opening_index=0,
            status="COMPLETED",
        )
        session.add(pair)
        session.flush()

        pgn_path = settings.run_root / "demo-match.pgn"
        pgn_path.parent.mkdir(parents=True, exist_ok=True)
        pgn_path.write_text(SAMPLE_PGN, encoding="utf-8")
        game = Game(
            id=str(uuid.uuid4()),
            tournament_id=tournament.id,
            pair_job_id=pair.id,
            game_number=1,
            white_engine="EngineA",
            black_engine="EngineB",
            opening_index=0,
            result="1-0",
            pgn_path=str(pgn_path),
            verified=True,
        )
        session.add(game)
        session.commit()
        gid = game.id

    os.environ["ARENA_DB_URL"] = settings.db_url
    os.environ["ARENA_RUN_ROOT"] = str(settings.run_root)
    os.environ["ARENA_BUILD_ROOT"] = str(settings.build_root)
    os.environ["ARENA_OPENING_ROOT"] = str(settings.opening_root)
    os.environ["ARENA_CUTECHESS"] = str(settings.cutechess)
    os.environ["ARENA_BASE_PATH"] = settings.base_path

    port = _free_port()
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "chessarena.main:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=str(ARENA_ROOT),
        env=dict(os.environ),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}/chessarena"
    try:
        _wait_until_up(f"{base}/games/{gid}")

        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            console_errors: list[str] = []

            def _on_console(msg):
                if msg.type == "error":
                    console_errors.append(msg.text)

            page.on("console", _on_console)
            page.on("pageerror", lambda exc: console_errors.append(str(exc)))

            resp = page.goto(
                f"{base}/games/{gid}", wait_until="networkidle"
            )
            assert resp.status == 200
            page.wait_for_timeout(600)

            board = page.locator(".board-wrap")
            assert board.count() == 1, "board not mounted"
            assert board.get_attribute("data-fen") == START_FEN, (
                "initial position wrong"
            )

            # Move count = 6 plies.
            moves = page.locator("button.move")
            assert moves.count() == 6, f"move count wrong: {moves.count()}"

            # Clicking ply 3 (2.exd5, the 3rd move button) sets the FEN.
            moves.nth(2).click()
            page.wait_for_timeout(300)
            assert board.get_attribute("data-fen") == PLY3_FEN

            # Next button -> ply 4 (2...Qxd5 capture).
            page.get_by_label("Next move").click()
            page.wait_for_timeout(300)
            assert board.get_attribute("data-fen") == PLY4_FEN

            # Last -> final position.
            page.locator("button", has_text="last").click()
            page.wait_for_timeout(300)
            assert board.get_attribute("data-fen") == PLY6_FEN

            # Previous -> ply 5.
            page.get_by_label("Previous move").click()
            page.wait_for_timeout(300)
            assert board.get_attribute("data-fen") == PLY5_FEN

            # First -> start.
            page.locator("button", has_text="first").click()
            page.wait_for_timeout(300)
            assert board.get_attribute("data-fen") == START_FEN

            # Keyboard right/left navigation.
            page.keyboard.press("ArrowRight")
            page.wait_for_timeout(200)
            assert board.get_attribute("data-fen") != START_FEN
            page.keyboard.press("ArrowLeft")
            page.wait_for_timeout(200)
            assert board.get_attribute("data-fen") == START_FEN

            # Metadata badges + player names.  White sits BELOW the board
            # (react-chessboard default white orientation), Black ABOVE.
            assert (
                page.locator(".player-card.top .player-name").inner_text()
                == "EngineB"
            )
            assert (
                page.locator(".player-card.bottom .player-name").inner_text()
                == "EngineA"
            )
            body_text = page.locator("body").inner_text()
            assert "Game 1" in body_text
            assert "Pair 1" in body_text
            assert "3+2" in body_text
            assert "blitz_3_2" not in body_text
            assert "1-0" in body_text

            # Mobile viewport: single column layout (moves below board).
            page.set_viewport_size({"width": 390, "height": 844})
            page.wait_for_timeout(300)
            cols = page.evaluate(
                "getComputedStyle(document.querySelector('.replay'))"
                ".gridTemplateColumns"
            )
            assert " " not in cols.strip(), (
                f"expected single column on mobile, got {cols}"
            )

            assert not console_errors, f"browser console errors: {console_errors}"
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_browser_demo_replay_from_fen_header(settings, engine_factory, registered):
    """PGNs from opening-book games carry a [FEN] header; the demo must
    start from that position, not the standard array (regression: 'Invalid
    move' during navigation on real tournament PGNs)."""
    import json

    from chessarena.models import PairJob

    manifest = json.loads(
        (registered["build_dir"] / "manifest.json").read_text(encoding="utf-8")
    )
    opening_manifest = json.loads(
        (registered["opening_dir"] / "manifest.json").read_text(encoding="utf-8")
    )

    with engine_factory() as session:
        tournament = Tournament(
            id=str(uuid.uuid4()),
            name="e2e-demo-fen",
            status=COMPLETED,
            engine_a_build_id=manifest["build_id"],
            engine_a_profile="current-final",
            engine_b_build_id=manifest["build_id"],
            engine_b_profile="current",
            opening_set_id=opening_manifest["opening_set_id"],
            time_control="blitz_3_2",
            requested_pairs=2,
            completed_pairs=2,
            config_snapshot={},
        )
        session.add(tournament)
        session.flush()
        pair = PairJob(
            id=str(uuid.uuid4()),
            tournament_id=tournament.id,
            pair_index=0,
            opening_index=0,
            status="COMPLETED",
        )
        session.add(pair)
        session.flush()
        pgn_path = settings.run_root / "demo-fen-match.pgn"
        pgn_path.parent.mkdir(parents=True, exist_ok=True)
        pgn_path.write_text(FEN_HEADER_PGN, encoding="utf-8")
        game = Game(
            id=str(uuid.uuid4()),
            tournament_id=tournament.id,
            pair_job_id=pair.id,
            game_number=1,
            white_engine="EngineA",
            black_engine="EngineB",
            opening_index=0,
            result="*",
            pgn_path=str(pgn_path),
            verified=True,
        )
        session.add(game)
        session.commit()
        gid = game.id

    os.environ["ARENA_DB_URL"] = settings.db_url
    os.environ["ARENA_RUN_ROOT"] = str(settings.run_root)
    os.environ["ARENA_BUILD_ROOT"] = str(settings.build_root)
    os.environ["ARENA_OPENING_ROOT"] = str(settings.opening_root)
    os.environ["ARENA_CUTECHESS"] = str(settings.cutechess)
    os.environ["ARENA_BASE_PATH"] = settings.base_path

    port = _free_port()
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "chessarena.main:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=str(ARENA_ROOT),
        env=dict(os.environ),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}/chessarena"
    try:
        _wait_until_up(f"{base}/games/{gid}")
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            console_errors: list[str] = []

            def _on_console(msg):
                if msg.type == "error":
                    console_errors.append(msg.text)

            page.on("console", _on_console)
            page.on("pageerror", lambda exc: console_errors.append(str(exc)))

            resp = page.goto(
                f"{base}/games/{gid}", wait_until="networkidle"
            )
            assert resp.status == 200
            page.wait_for_timeout(600)

            board = page.locator(".board-wrap")
            # Initial position = the [FEN] header, NOT the standard array.
            assert board.get_attribute("data-fen") == FEN_START
            moves = page.locator("button.move")
            assert moves.count() == 6

            # Click ply 2 (1...a6) -> position after that ply.
            moves.nth(1).click()
            page.wait_for_timeout(300)
            assert board.get_attribute("data-fen") == FEN_PLY2

            assert not console_errors, f"browser console errors: {console_errors}"
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_browser_demo_long_game_active_move_scrolls_into_view(
    settings, engine_factory, registered
):
    """P2 polish: navigating a long game keeps the active move visible inside
    the scrollable move list."""
    import json

    from chessarena.models import PairJob

    manifest = json.loads(
        (registered["build_dir"] / "manifest.json").read_text(encoding="utf-8")
    )
    opening_manifest = json.loads(
        (registered["opening_dir"] / "manifest.json").read_text(encoding="utf-8")
    )

    with engine_factory() as session:
        tournament = Tournament(
            id=str(uuid.uuid4()),
            name="e2e-demo-long",
            status=COMPLETED,
            engine_a_build_id=manifest["build_id"],
            engine_a_profile="current-final",
            engine_b_build_id=manifest["build_id"],
            engine_b_profile="current",
            opening_set_id=opening_manifest["opening_set_id"],
            time_control="blitz_3_2",
            requested_pairs=2,
            completed_pairs=2,
            config_snapshot={},
        )
        session.add(tournament)
        session.flush()
        pair = PairJob(
            id=str(uuid.uuid4()),
            tournament_id=tournament.id,
            pair_index=0,
            opening_index=0,
            status="COMPLETED",
        )
        session.add(pair)
        session.flush()
        pgn_path = settings.run_root / "demo-long-match.pgn"
        pgn_path.parent.mkdir(parents=True, exist_ok=True)
        pgn_path.write_text(LONG_PGN, encoding="utf-8")
        game = Game(
            id=str(uuid.uuid4()),
            tournament_id=tournament.id,
            pair_job_id=pair.id,
            game_number=1,
            white_engine="EngineA",
            black_engine="EngineB",
            opening_index=0,
            result="*",
            pgn_path=str(pgn_path),
            verified=True,
        )
        session.add(game)
        session.commit()
        gid = game.id

    os.environ["ARENA_DB_URL"] = settings.db_url
    os.environ["ARENA_RUN_ROOT"] = str(settings.run_root)
    os.environ["ARENA_BUILD_ROOT"] = str(settings.build_root)
    os.environ["ARENA_OPENING_ROOT"] = str(settings.opening_root)
    os.environ["ARENA_CUTECHESS"] = str(settings.cutechess)
    os.environ["ARENA_BASE_PATH"] = settings.base_path

    port = _free_port()
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "chessarena.main:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=str(ARENA_ROOT),
        env=dict(os.environ),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}/chessarena"
    try:
        _wait_until_up(f"{base}/games/{gid}")
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            console_errors: list[str] = []

            def _on_console(msg):
                if msg.type == "error":
                    console_errors.append(msg.text)

            page.on("console", _on_console)
            page.on("pageerror", lambda exc: console_errors.append(str(exc)))

            resp = page.goto(
                f"{base}/games/{gid}", wait_until="networkidle"
            )
            assert resp.status == 200
            page.wait_for_timeout(600)

            moves = page.locator("button.move")
            assert moves.count() == 80

            # Jump to the final ply; the active move must be visible inside
            # the move-list viewport.
            page.locator("button", has_text="last").click()
            page.wait_for_timeout(800)  # allow smooth scroll to settle

            active_visible = page.evaluate(
                """() => {
                    const list = document.querySelector('.moves-list');
                    const active = document.querySelector('.move.active');
                    if (!list || !active) return false;
                    const lr = list.getBoundingClientRect();
                    const ar = active.getBoundingClientRect();
                    return ar.top >= lr.top - 1 && ar.bottom <= lr.bottom + 1;
                }"""
            )
            assert active_visible, "active move not visible in move-list viewport"

            assert not console_errors, f"browser console errors: {console_errors}"
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_browser_replay_analysis_eval_bar(settings, engine_factory, registered):
    """P4.7: with an analysis artifact present, the eval panel + bar render
    and the score follows ←/→ navigation."""
    import json

    manifest = json.loads(
        (registered["build_dir"] / "manifest.json").read_text(encoding="utf-8")
    )
    opening_manifest = json.loads(
        (registered["opening_dir"] / "manifest.json").read_text(encoding="utf-8")
    )

    with engine_factory() as session:
        tournament = Tournament(
            id=str(uuid.uuid4()),
            name="e2e-analysis",
            status=COMPLETED,
            engine_a_build_id=manifest["build_id"],
            engine_a_profile="current-final",
            engine_b_build_id=manifest["build_id"],
            engine_b_profile="current",
            opening_set_id=opening_manifest["opening_set_id"],
            time_control="blitz_3_2",
            requested_pairs=1,
            completed_pairs=1,
            config_snapshot={
                "engine_a": {"build_id": manifest["build_id"], "profile": "current-final"},
                "engine_b": {"build_id": manifest["build_id"], "profile": "current"},
                "opening_set": {"opening_set_id": opening_manifest["opening_set_id"]},
                "time_control": "blitz_3_2",
            },
        )
        session.add(tournament)
        session.flush()
        from chessarena.models import PairJob

        pair = PairJob(
            id=str(uuid.uuid4()),
            tournament_id=tournament.id,
            pair_index=0,
            opening_index=0,
            status="COMPLETED",
        )
        session.add(pair)
        session.flush()

        pgn_path = settings.run_root / "analysis-match.pgn"
        pgn_path.parent.mkdir(parents=True, exist_ok=True)
        pgn_path.write_text(SAMPLE_PGN, encoding="utf-8")
        game = Game(
            id=str(uuid.uuid4()),
            tournament_id=tournament.id,
            pair_job_id=pair.id,
            game_number=1,
            white_engine="EngineA",
            black_engine="EngineB",
            opening_index=0,
            result="1-0",
            pgn_path=str(pgn_path),
            verified=True,
        )
        session.add(game)
        session.commit()
        gid = game.id
        tid = tournament.id

    # Write an analysis artifact covering plies 0..6 (6 moves in SAMPLE_PGN).
    # Scores crafted so move 1 (e4, White) and move 4 (Qxd5, Black) are clear
    # winning-share blunders (loss >= 0.35), move 5 (Nc3) a mistake; the
    # biggest swing is move 4.  The final position has NO evaluation: unknown
    # must never be classified as a blunder or an exactly equal position.
    scores = [50, -350, -300, -180, 250, -20, None]
    positions = []
    board = chess.Board()
    fens = [board.fen()]
    for uci in ["e2e4", "d7d5", "e4d5", "d8d5", "b1c3", "d5d8"]:
        board.push_uci(uci)
        fens.append(board.fen())
    for ply, (fen, cp) in enumerate(zip(fens, scores)):
        positions.append(
            {"ply": ply, "fen": fen, "score_cp": cp, "mate": None,
             "best_move": "g1f3", "pv": ["g1f3", "g8f6"]}
        )
    analysis_dir = settings.run_root / tid / "analysis"
    analysis_dir.mkdir(parents=True, exist_ok=True)
    (analysis_dir / f"{gid}.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "game_id": gid,
                "engine": {"name": "Stockfish", "build_id": "x", "binary_sha256": "y"},
                "limit": {"type": "nodes", "value": 100000},
                "positions": positions,
            }
        ),
        encoding="utf-8",
    )

    os.environ["ARENA_DB_URL"] = settings.db_url
    os.environ["ARENA_RUN_ROOT"] = str(settings.run_root)
    os.environ["ARENA_BUILD_ROOT"] = str(settings.build_root)
    os.environ["ARENA_OPENING_ROOT"] = str(settings.opening_root)
    os.environ["ARENA_CUTECHESS"] = str(settings.cutechess)
    os.environ["ARENA_BASE_PATH"] = settings.base_path

    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "chessarena.main:create_app",
         "--factory", "--host", "127.0.0.1", "--port", str(port),
         "--log-level", "warning"],
        cwd=str(ARENA_ROOT),
        env=dict(os.environ),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}/chessarena"
    try:
        _wait_until_up(f"{base}/games/{gid}")

        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            console_errors: list[str] = []

            def _on_console(msg):
                if msg.type == "error":
                    console_errors.append(msg.text)

            page.on("console", _on_console)
            page.on("pageerror", lambda exc: console_errors.append(str(exc)))

            resp = page.goto(f"{base}/games/{gid}", wait_until="networkidle")
            assert resp.status == 200
            page.wait_for_timeout(800)

            print("CONSOLE_ERRORS=" + repr(console_errors))
            assert page.locator(".eval-bar").count() == 1, "eval bar missing"
            assert page.locator(".analysis-panel").count() == 1, "analysis panel missing"
            score = page.locator(".analysis-score")
            assert "+0.50" in score.inner_text(), "ply 0 score wrong"

            # ArrowRight steps one ply -> score follows the analysis.
            page.keyboard.press("ArrowRight")
            page.wait_for_timeout(200)
            assert "-3.50" in score.inner_text(), "ply 1 score wrong"
            page.keyboard.press("ArrowLeft")
            page.wait_for_timeout(200)
            assert "+0.50" in score.inner_text(), "ply 0 score wrong after back"

            # P4.9b/c: move marks — ply 1 (White e4) ??, ply 4 (Black Qxd5) ??,
            # ply 5 (White Nc3) ? (mistake).  The final move (ply 6) has an
            # unknown evaluation and must NOT get a mark.
            marks = page.locator(".move-mark")
            assert marks.count() == 3, f"expected 3 move marks, got {marks.count()}"
            assert marks.nth(0).inner_text() == "??"
            assert marks.nth(1).inner_text() == "??"
            assert marks.nth(2).inner_text() == "?"
            # Unknown eval on the final ply renders as a dash, not "-M0" or 0.
            page.keyboard.press("End")
            page.wait_for_timeout(200)
            assert score.inner_text().strip().startswith("\u2014"), (
                f"unknown eval should render as dash, got {score.inner_text()!r}"
            )

            # Biggest swing button jumps to ply 4 (Qxd5, the larger drop).
            biggest_btn = page.locator(".analysis-actions button", has_text="Biggest swing")
            assert "Qxd5" in biggest_btn.inner_text()
            biggest_btn.click()
            page.wait_for_timeout(200)
            assert page.locator(".ply-indicator").inner_text() == "4/6"
            assert "+2.50" in score.inner_text()

            # Error navigation: next/previous between plies 1 and 4.
            page.locator(".analysis-actions button", has_text="\u2039 Error").click()
            page.wait_for_timeout(200)
            assert page.locator(".ply-indicator").inner_text() == "1/6"
            page.locator(".analysis-actions button", has_text="Error \u203a").click()
            page.wait_for_timeout(200)
            assert page.locator(".ply-indicator").inner_text() == "4/6"

            # P4.5a repair: pause -> play resumes from the current ply instead
            # of restarting from the start; play from the final position does
            # restart from ply 0.
            page.keyboard.press("Home")  # ply 0
            page.keyboard.press("ArrowRight")  # ply 1
            page.keyboard.press("ArrowRight")  # ply 2
            page.locator("button", has_text="\u25b6").click()  # play
            page.wait_for_timeout(120)
            page.locator("button", has_text="\u23f8").click()  # pause
            resumed = page.locator(".ply-indicator").inner_text()
            assert resumed.startswith("2/"), f"resume reset to start: {resumed}"
            # Play again from the middle keeps the position (no reset).
            page.locator("button", has_text="\u25b6").click()
            page.wait_for_timeout(120)
            page.locator("button", has_text="\u23f8").click()
            resumed2 = page.locator(".ply-indicator").inner_text()
            assert resumed2.startswith("2/"), f"second resume reset: {resumed2}"
            # From the final position, play restarts from ply 0.
            page.keyboard.press("End")
            page.locator("button", has_text="\u25b6").click()
            page.wait_for_timeout(120)
            page.locator("button", has_text="\u23f8").click()
            assert page.locator(".ply-indicator").inner_text() == "0/6", (
                "play from the end must restart from ply 0"
            )

            assert not console_errors, f"browser console errors: {console_errors}"
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

def test_browser_match_filters(settings, engine_factory, registered):
    """P4.9a: the completed-match games table filters by decisive/win/loss/
    draw/analyzed, with counts, purely client side."""
    import json

    manifest = json.loads(
        (registered["build_dir"] / "manifest.json").read_text(encoding="utf-8")
    )
    opening_manifest = json.loads(
        (registered["opening_dir"] / "manifest.json").read_text(encoding="utf-8")
    )

    with engine_factory() as session:
        tournament = Tournament(
            id=str(uuid.uuid4()),
            name="e2e-filters",
            status=COMPLETED,
            engine_a_build_id=manifest["build_id"],
            engine_a_profile="current-final",
            engine_b_build_id=manifest["build_id"],
            engine_b_profile="current",
            opening_set_id=opening_manifest["opening_set_id"],
            time_control="blitz_3_2",
            requested_pairs=3,
            completed_pairs=3,
            config_snapshot={
                # Both sides share a display name: the filter must still tell
                # Engine A apart via the pair color contract (game_number
                # parity), never via display-name comparison.
                "engine_a": {"display_name": "SameEngine",
                             "build_id": manifest["build_id"], "profile": "current-final"},
                "engine_b": {"display_name": "SameEngine",
                             "build_id": manifest["build_id"], "profile": "current"},
                "opening_set": {"opening_set_id": opening_manifest["opening_set_id"]},
                "time_control": "blitz_3_2",
            },
        )
        session.add(tournament)
        session.flush()
        from chessarena.models import PairJob

        pair = PairJob(
            id=str(uuid.uuid4()),
            tournament_id=tournament.id,
            pair_index=0,
            opening_index=0,
            status="COMPLETED",
        )
        session.add(pair)
        session.flush()

        games_spec = [
            # game_number odd -> Engine A is White.
            ("SameEngine", "SameEngine", "1-0", True),      # A white wins
            ("SameEngine", "SameEngine", "1-0", False),     # A black loses
            ("SameEngine", "SameEngine", "1/2-1/2", False), # draw
        ]
        gids = []
        for i, (white, black, result, analyzed) in enumerate(games_spec):
            g = Game(
                id=str(uuid.uuid4()),
                tournament_id=tournament.id,
                pair_job_id=pair.id,
                game_number=i + 1,
                white_engine=white,
                black_engine=black,
                opening_index=0,
                result=result,
                pgn_path=str(settings.run_root / f"filter-{i}.pgn"),
                verified=True,
            )
            session.add(g)
            session.flush()
            gids.append(g.id)
            if analyzed:
                d = settings.run_root / tournament.id / "analysis"
                d.mkdir(parents=True, exist_ok=True)
                (d / f"{g.id}.json").write_text("{}", encoding="utf-8")
        session.commit()
        tid = tournament.id

    os.environ["ARENA_DB_URL"] = settings.db_url
    os.environ["ARENA_RUN_ROOT"] = str(settings.run_root)
    os.environ["ARENA_BUILD_ROOT"] = str(settings.build_root)
    os.environ["ARENA_OPENING_ROOT"] = str(settings.opening_root)
    os.environ["ARENA_CUTECHESS"] = str(settings.cutechess)
    os.environ["ARENA_BASE_PATH"] = settings.base_path

    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "chessarena.main:create_app",
         "--factory", "--host", "127.0.0.1", "--port", str(port),
         "--log-level", "warning"],
        cwd=str(ARENA_ROOT),
        env=dict(os.environ),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}/chessarena"
    try:
        _wait_until_up(f"{base}/matches/{tid}")

        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            console_errors: list[str] = []

            def _on_console(msg):
                if msg.type == "error":
                    console_errors.append(msg.text)

            page.on("console", _on_console)
            page.on("pageerror", lambda exc: console_errors.append(str(exc)))

            resp = page.goto(f"{base}/matches/{tid}", wait_until="networkidle")
            assert resp.status == 200
            page.wait_for_timeout(400)

            rows = page.locator("#game-filters + table tbody tr")
            assert rows.count() == 3

            def visible_count():
                return rows.evaluate_all(
                    "els => els.filter(e => e.style.display !== 'none').length"
                )

            def click_filter(label):
                page.locator("#game-filters .gf", has_text=label).click()
                page.wait_for_timeout(200)

            assert visible_count() == 3  # All
            click_filter("Decisive")
            assert visible_count() == 2
            click_filter("Wins")
            assert visible_count() == 1
            click_filter("Losses")
            assert visible_count() == 1
            click_filter("Draws")
            assert visible_count() == 1
            click_filter("Analyzed")
            assert visible_count() == 1
            click_filter("All")
            assert visible_count() == 3

            assert not console_errors, f"browser console errors: {console_errors}"
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

def test_browser_custom_elo_hidden_input_cleared(settings, engine_factory, registered):
    """P4.6 repair: switching to a preset whose build lacks UCI_Elo clears and
    disables the hidden Custom Elo input so a stale value is never submitted."""
    import json

    manifest = json.loads(
        (registered["build_dir"] / "manifest.json").read_text(encoding="utf-8")
    )
    opening_manifest = json.loads(
        (registered["opening_dir"] / "manifest.json").read_text(encoding="utf-8")
    )
    with engine_factory() as session:
        from chessarena.models import EngineBuild, EnginePreset

        elo_build = session.query(EngineBuild).first()
        elo_build.uci_options_schema = {
            "UCI_LimitStrength": {"type": "check", "default": "false"},
            "UCI_Elo": {"type": "spin", "default": "1350", "min": 1, "max": 2850},
        }
        no_elo = EngineBuild(
            build_id="no-elo-build",
            engine_name="NoEloEngine",
            git_sha="external",
            binary_path="/unused/noelo",
            binary_sha256="b" * 64,
            platform="linux-x86_64",
            supported_profiles=[],
            manifest={},
            enabled=True,
            uci_options_schema={"Hash": {"type": "spin"}},
        )
        session.add(no_elo)
        session.add(
            EnginePreset(
                preset_id="no-elo-preset",
                build_id="no-elo-build",
                display_name="No Elo Engine",
                command_args=[],
                uci_options={},
                category="custom",
                public_visible=True,
                enabled=True,
            )
        )
        session.commit()

    os.environ["ARENA_DB_URL"] = settings.db_url
    os.environ["ARENA_RUN_ROOT"] = str(settings.run_root)
    os.environ["ARENA_BUILD_ROOT"] = str(settings.build_root)
    os.environ["ARENA_OPENING_ROOT"] = str(settings.opening_root)
    os.environ["ARENA_CUTECHESS"] = str(settings.cutechess)
    os.environ["ARENA_BASE_PATH"] = settings.base_path

    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "chessarena.main:create_app",
         "--factory", "--host", "127.0.0.1", "--port", str(port),
         "--log-level", "warning"],
        cwd=str(ARENA_ROOT),
        env=dict(os.environ),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}/chessarena"
    try:
        _wait_until_up(f"{base}/admin/tournaments/new")

        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            console_errors: list[str] = []

            def _on_console(msg):
                if msg.type == "error":
                    console_errors.append(msg.text)

            page.on("console", _on_console)
            page.on("pageerror", lambda exc: console_errors.append(str(exc)))

            resp = page.goto(f"{base}/admin/tournaments/new", wait_until="networkidle")
            assert resp.status == 200
            page.wait_for_timeout(300)

            elo_input = page.locator('input[name="engine_b_elo"]')
            # Select an Elo-capable preset, type a custom Elo.
            page.select_option('select[name="engine_b_preset"]', "chessengine-legacy-current")
            page.wait_for_timeout(150)
            assert elo_input.is_visible()
            elo_input.fill("1850")

            # Switch to a preset without UCI_Elo: input hidden, cleared, disabled.
            page.select_option('select[name="engine_b_preset"]', "no-elo-preset")
            page.wait_for_timeout(150)
            assert not elo_input.is_visible()
            assert elo_input.input_value() == ""
            assert elo_input.is_disabled()

            # Switching back re-enables it.
            page.select_option('select[name="engine_b_preset"]', "chessengine-legacy-current")
            page.wait_for_timeout(150)
            assert elo_input.is_visible()
            assert not elo_input.is_disabled()

            assert not console_errors, f"browser console errors: {console_errors}"
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
