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

# A fake engine that reproduces the UCI out-of-order race: on a FEN switch
# the hook sends "stop" (and, in older code, "isready").  The fake answers
# isready with readyok IMMEDIATELY (so a readyok-based barrier starts the new
# FEN), and ~50ms later — after the new FEN's genuine info has been posted —
# emits the OLD search's late info +5.00 and its bestmove.  A correct
# stop/bestmove barrier drops that stale info; a readyok barrier accepts it
# and the +5.00 sticks to the new FEN's panel.
FAKE_STALE_OUTPUT_WORKER = (
    "let stopSeen = false;\n"
    "function staleOutput() {\n"
    "  self.postMessage('info depth 7 score cp 500 nodes 2 nps 1 time 2 pv b1c3');\n"
    "  self.postMessage('bestmove b1c3');\n"
    "}\n"
    "self.onmessage = (e) => {\n"
    "  const msg = e.data;\n"
    "  if (msg === 'uci') {\n"
    "    self.postMessage('Stockfish 2019-08-15 FakeEngine');\n"
    "    self.postMessage('uciok');\n"
    "  } else if (msg === 'isready') {\n"
    "    self.postMessage('readyok');\n"
    "  } else if (msg.startsWith('go')) {\n"
    "    self.postMessage('info depth 6 score cp 100 nodes 1 nps 1 time 1 pv a2a3');\n"
    "  } else if (msg === 'stop') {\n"
    "    stopSeen = true;\n"
    "    setTimeout(() => {\n"
    "      if (stopSeen) { stopSeen = false; staleOutput(); }\n"
    "    }, 50);\n"
    "  }\n"
    "};\n"
)


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
            page.wait_for_timeout(2000)  # allow smooth scroll to settle

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
            if not active_visible:
                rects = page.evaluate(
                    """() => {
                        const list = document.querySelector('.moves-list');
                        const active = document.querySelector('.move.active');
                        const lr = list.getBoundingClientRect();
                        const ar = active.getBoundingClientRect();
                        return { listTop: lr.top, listBottom: lr.bottom,
                                 activeTop: ar.top, activeBottom: ar.bottom,
                                 listScrollTop: list.scrollTop, listScrollHeight: list.scrollHeight,
                                 listClientHeight: list.clientHeight };
                    }"""
                )
                print("SCROLL_STATE=" + repr(rects))
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
            score = page.locator(".diagnostics-title")
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
            # Unknown eval on the final ply shows no score in the diagnostics
            # title (and never "-M0").
            page.keyboard.press("End")
            page.wait_for_timeout(200)
            title = score.inner_text()
            assert "Game diagnostics" in title
            assert "-M0" not in title, f"mate 0 rendered as a score: {title!r}"

            # Biggest swing button jumps to ply 4 (Qxd5, the larger drop).
            try:
                biggest_btn = page.locator(".diagnostics-actions button", has_text="Biggest swing")
                assert "Qxd5" in biggest_btn.inner_text()
            except Exception as exc:
                print("CONSOLE_ERR=" + repr(console_errors))
                print("BODY_ERR=" + repr(page.locator("body").inner_text()[:400]))
                raise exc
            biggest_btn.click()
            page.wait_for_timeout(200)
            assert page.locator(".ply-indicator").inner_text() == "4/6"
            assert "+2.50" in score.inner_text()

            # Error navigation: next/previous between plies 1 and 4.
            page.locator(".diagnostics-actions button", has_text="\u2039 Error").click()
            page.wait_for_timeout(200)
            assert page.locator(".ply-indicator").inner_text() == "1/6"
            page.locator(".diagnostics-actions button", has_text="Error \u203a").click()
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
            click_filter("Diagnostics")
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

BTM_PGN = "\n".join(
    [
        '[Event "E2E"]',
        '[Site "?"]',
        '[Date "2026.08.09"]',
        '[Round "1"]',
        '[White "EngineA"]',
        '[Black "EngineB"]',
        '[Result "1-0"]',
        '[FEN "r1bqkb1r/1ppp1ppp/p1n2n2/4p3/B3P3/5N2/PPPP1PPP/RNBQ1RK1 b kq - 3 5"]',
        "",
        "5...d6 6. d3 Bg4 1-0",
        "",
    ]
)
BTM_START_FEN = "r1bqkb1r/1ppp1ppp/p1n2n2/4p3/B3P3/5N2/PPPP1PPP/RNBQ1RK1 b kq - 3 5"


def test_browser_replay_black_to_move_fen(settings, engine_factory, registered):
    """P4.9 final repair: with a Black-to-move start FEN the first move is
    scored as Black's move (mark on a Black blunder), and move labels/rows use
    the real fullmove number from the FEN (5...d6, 6.d3)."""
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
            name="e2e-btm",
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
                "engine_a": {"display_name": "EngineA", "build_id": manifest["build_id"]},
                "engine_b": {"display_name": "EngineB", "build_id": manifest["build_id"]},
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
        pgn_path = settings.run_root / "btm-match.pgn"
        pgn_path.parent.mkdir(parents=True, exist_ok=True)
        pgn_path.write_text(BTM_PGN, encoding="utf-8")
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

    # Analysis: move 1 is BLACK's 5...d6 and is a clear black blunder (share
    # jumps 0.53 -> 0.73); moves 2/3 are quiet.
    scores = [30, 250, 240, 245]
    positions = []
    board = chess.Board(BTM_START_FEN)
    fens = [board.fen()]
    for uci in ["d7d6", "d2d3", "c8g4"]:
        board.push_uci(uci)
        fens.append(board.fen())
    for ply, (fen, cp) in enumerate(zip(fens, scores)):
        positions.append(
            {"ply": ply, "fen": fen, "score_cp": cp, "mate": None,
             "best_move": "d7d6", "pv": ["d7d6"]}
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

            # The first move is Black's and must carry the ? mark (the old
            # ply-parity logic would have scored it as White and dropped it).
            marks = page.locator(".move-mark")
            assert marks.count() == 1, f"expected 1 mark, got {marks.count()}"
            assert marks.nth(0).inner_text() == "?"

            # Move list rows use the real fullmove numbers from the FEN.
            move_ns = page.locator(".move-n").all_inner_texts()
            assert move_ns == ["5.", "6."], f"move numbers wrong: {move_ns}"

            # Biggest swing is the black blunder and shows the full label.
            biggest_btn = page.locator(
                ".diagnostics-actions button", has_text="Biggest swing"
            )
            assert "5...d6" in biggest_btn.inner_text()
            biggest_btn.click()
            page.wait_for_timeout(200)
            assert page.locator(".ply-indicator").inner_text() == "1/3"

            assert not console_errors, f"browser console errors: {console_errors}"
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

def test_browser_replay_interactive_stockfish(settings, engine_factory, registered):
    """P4.11 commit 2: any verified game gets interactive browser Stockfish
    analysis without any server diagnostics artifact; the eval updates when
    navigating plies."""
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
            name="e2e-stockfish",
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
                "engine_a": {"display_name": "EngineA", "build_id": manifest["build_id"]},
                "engine_b": {"display_name": "EngineB", "build_id": manifest["build_id"]},
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
        pgn_path = settings.run_root / "sf-match.pgn"
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

            # Count page-side "go ..." commands so the search lifecycle is
            # observable: one FEN must trigger exactly one search, and no
            # bestmove-driven restart loop may re-issue "go".
            page.add_init_script(
                """
                window.__goCount = 0;
                const __origPost = Worker.prototype.postMessage;
                Worker.prototype.postMessage = function (msg) {
                  if (typeof msg === 'string' && msg.startsWith('go ')) {
                    window.__goCount += 1;
                  }
                  return __origPost.apply(this, arguments);
                };
                """
            )

            resp = page.goto(f"{base}/games/{gid}", wait_until="domcontentloaded")
            assert resp.status == 200

            # The browser Stockfish panel must appear without any server
            # diagnostics artifact, and produce a score within 30s.
            panel = page.locator(".analysis-panel")
            panel.wait_for(state="visible", timeout=20000)
            assert "Stockfish" in panel.inner_text()
            page.wait_for_function(
                """() => {
                    const el = document.querySelector('.analysis-score');
                    return el && !el.innerText.startsWith('\u2026');
                }""",
                timeout=30000,
            )
            first_score = page.locator(".analysis-score").inner_text()

            # P1 regression: exactly one search for the first FEN, and the
            # engine keeps deepening (go infinite) instead of restarting.
            assert page.evaluate("window.__goCount") == 1, (
                f"expected 1 go for the first FEN, got {page.evaluate('window.__goCount')}"
            )
            page.wait_for_timeout(3000)
            assert page.evaluate("window.__goCount") == 1, (
                "no restart loop allowed: go count grew while the position "
                f"was unchanged -> {page.evaluate('window.__goCount')}"
            )
            line_text = page.locator(".analysis-line").inner_text()
            d1 = __import__("re").search(r"d(\d+)", line_text)
            assert d1, f"panel must show a depth: {line_text!r}"
            page.wait_for_timeout(2500)
            line_text2 = page.locator(".analysis-line").inner_text()
            d2 = __import__("re").search(r"d(\d+)", line_text2)
            assert d2 and int(d2.group(1)) >= int(d1.group(1)), (
                "go infinite must keep deepening: depth went backwards "
                f"{d1.group(1)} -> {d2 and d2.group(1)}"
            )

            # Engine provenance: the id name from the worker (Stockfish
            # <date>) is shown in the panel, separate from the server engine.
            engine_label = page.locator(".analysis-engine").inner_text()
            assert __import__("re").match(
                r"^Stockfish \d{4}-\d{2}-\d{2} · browser", engine_label
            ), f"expected versioned engine label, got {engine_label!r}"

            # Navigating a ply re-analyzes the new position (the score panel
            # is still present and the search indicator shows at some point).
            page.keyboard.press("ArrowRight")
            page.wait_for_timeout(500)
            assert page.locator(".analysis-panel").count() == 1
            page.wait_for_function(
                """() => {
                    const el = document.querySelector('.analysis-score');
                    return el && !el.innerText.startsWith('\u2026');
                }""",
                timeout=30000,
            )
            second_score = page.locator(".analysis-score").inner_text()
            # A different ply usually yields a different eval; at minimum the
            # panel must be live (not the server artifact, which is absent).
            assert "Stockfish" in panel.inner_text()

            # P1 regression: one FEN -> exactly one new search.
            assert page.evaluate("window.__goCount") == 2, (
                f"expected 2 go commands after one ply navigation, "
                f"got {page.evaluate('window.__goCount')}"
            )

            # P2 regression: the engine version must survive a FEN change
            # (each navigation used to replace the whole state, dropping it).
            engine_label_after = page.locator(".analysis-engine").inner_text()
            assert engine_label_after == engine_label, (
                f"version label lost after navigation: {engine_label!r} -> "
                f"{engine_label_after!r}"
            )

            # P1 regression: fast A -> B -> A must re-serve A as a NEW
            # generation and re-show its score (never stall in "searching").
            page.keyboard.press("ArrowLeft")
            page.wait_for_timeout(500)
            assert page.locator(".analysis-panel").count() == 1
            page.wait_for_function(
                """() => {
                    const el = document.querySelector('.analysis-score');
                    return el && !el.innerText.startsWith('\u2026');
                }""",
                timeout=30000,
            )
            assert page.evaluate("window.__goCount") == 3, (
                "A -> B -> A must start a third search (one per FEN), "
                f"got {page.evaluate('window.__goCount')}"
            )
            assert page.locator(".analysis-engine").inner_text() == engine_label

            assert not console_errors, f"browser console errors: {console_errors}"

            # P2 regression: a worker that fails to load surfaces a visible
            # "unavailable" state instead of a silent blank panel.
            bad_page = browser.new_page()
            bad_errors: list[str] = []
            bad_page.on("pageerror", lambda exc: bad_errors.append(str(exc)))
            bad_page.route(
                "**/stockfish.wasm.js", lambda route: route.abort()
            )
            bad_page.goto(f"{base}/games/{gid}", wait_until="domcontentloaded")
            err_panel = bad_page.locator(".analysis-panel")
            err_panel.wait_for(state="visible", timeout=20000)
            assert "unavailable" in err_panel.inner_text().lower(), (
                f"expected 'Stockfish unavailable', got {err_panel.inner_text()!r}"
            )
            bad_page.close()

            # P2 regression: a worker that loads but NEVER answers "uci"
            # (hung init, no error event) must still surface the unavailable
            # state after the init timeout — not search forever.
            hung_page = browser.new_page()
            hung_errors: list[str] = []
            hung_page.on("pageerror", lambda exc: hung_errors.append(str(exc)))
            hung_page.route(
                "**/stockfish.wasm.js",
                lambda route: route.fulfill(
                    status=200,
                    content_type="text/javascript",
                    body="// deliberately silent worker stub",
                ),
            )
            hung_page.goto(f"{base}/games/{gid}", wait_until="domcontentloaded")
            hung_panel = hung_page.locator(".analysis-panel")
            # The panel shows "searching" right away; the init timeout must
            # flip it to the unavailable state ~10s later.
            hung_panel.wait_for(state="visible", timeout=20000)
            hung_page.wait_for_function(
                """() => {
                    const el = document.querySelector('.analysis-panel');
                    return el && el.innerText.includes('unavailable');
                }""",
                timeout=25000,
            )
            assert "unavailable" in hung_panel.inner_text().lower(), (
                f"hung worker must show 'Stockfish unavailable', "
                f"got {hung_panel.inner_text()!r}"
            )
            hung_page.close()

            # P1 regression: UCI "stop" does not drain the old search.  A
            # fake engine answers the FEN switch with readyok first (the old
            # code starts the new FEN right there) and only then emits the
            # OLD search's late info +5.00 and bestmove.  That stale output
            # must never land in the new FEN's panel.
            fake_page = browser.new_page()
            fake_errors: list[str] = []
            fake_page.on("pageerror", lambda exc: fake_errors.append(str(exc)))
            fake_page.route(
                "**/stockfish.wasm.js",
                lambda route: route.fulfill(
                    status=200,
                    content_type="text/javascript",
                    body=FAKE_STALE_OUTPUT_WORKER,
                ),
            )
            fake_page.goto(f"{base}/games/{gid}", wait_until="domcontentloaded")
            fake_panel = fake_page.locator(".analysis-panel")
            fake_panel.wait_for(state="visible", timeout=20000)
            fake_page.wait_for_function(
                """() => {
                    const el = document.querySelector('.analysis-score');
                    return el && el.innerText.includes('1.00');
                }""",
                timeout=20000,
            )
            fake_page.keyboard.press("ArrowRight")
            fake_page.wait_for_timeout(1200)
            score_text = fake_page.locator(".analysis-score").inner_text()
            assert "1.00" in score_text, (
                f"new FEN must show its genuine score, got {score_text!r}"
            )
            assert "5.00" not in score_text, (
                "stale old-search info leaked into the new FEN: "
                f"{score_text!r}"
            )
            assert "Stockfish 2019-08-15" in (
                fake_page.locator(".analysis-engine").inner_text()
            )
            fake_page.close()

            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_browser_live_fails_closed_without_colors(
    settings, engine_factory, registered
):
    """P4.11 closure repair: when the backend has no authoritative game
    boundary (no Started game line) it sends white/black = null, and the Live
    page must NOT guess engine A/B back into the player cards."""
    import json

    manifest = json.loads(
        (registered["build_dir"] / "manifest.json").read_text(encoding="utf-8")
    )
    opening_manifest = json.loads(
        (registered["opening_dir"] / "manifest.json").read_text(encoding="utf-8")
    )

    from chessarena.models import RUNNING, Tournament, utcnow

    with engine_factory() as session:
        tournament = Tournament(
            id=str(uuid.uuid4()),
            name="live-failclosed",
            status=RUNNING,
            engine_a_build_id=manifest["build_id"],
            engine_a_profile="current-final",
            engine_b_build_id=manifest["build_id"],
            engine_b_profile="current",
            opening_set_id=opening_manifest["opening_set_id"],
            time_control="blitz_3_2",
            requested_pairs=1,
            config_snapshot={
                "engine_a": {"display_name": "EngineA", "build_id": manifest["build_id"]},
                "engine_b": {"display_name": "EngineB", "build_id": manifest["build_id"]},
                "opening_set": {"opening_set_id": opening_manifest["opening_set_id"]},
                "time_control": "blitz_3_2",
            },
        )
        session.add(tournament)
        session.flush()
        tournament.started_at = utcnow()
        from chessarena.models import PairJob

        pair = PairJob(
            id=str(uuid.uuid4()),
            tournament_id=tournament.id,
            pair_index=0,
            opening_index=0,
            status=RUNNING,
        )
        session.add(pair)
        session.flush()
        run_dir = settings.run_root / tournament.id / "pairs" / "000000" / "attempt-01"
        run_dir.mkdir(parents=True, exist_ok=True)
        opening = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
        (run_dir / "opening.epd").write_text(opening + "\n", encoding="utf-8")
        # A debug stream WITHOUT any "Started game N" boundary line: the
        # backend must fail closed (white/black null).
        (run_dir / "stdout.log").write_text(
            f"4 >EngineA(0): position fen {opening}\n"
            "6 >EngineA(0): go wtime 180000 btime 180000 winc 2000 binc 2000\n"
            "8 <EngineA(0): info depth 10 score cp 99 nodes 100 nps 1000 time 5 pv d2d4\n",
            encoding="utf-8",
        )
        pair.run_directory = str(run_dir)
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
        _wait_until_up(f"{base}/live?tournament_id={tid}")

        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            console_errors: list[str] = []
            page.on("pageerror", lambda exc: console_errors.append(str(exc)))
            page.goto(f"{base}/live?tournament_id={tid}", wait_until="domcontentloaded")
            top_name = page.locator(".player-card.top .player-name")
            top_name.wait_for(state="visible", timeout=20000)
            page.wait_for_function(
                """() => {
                    const el = document.querySelector('.player-card.top .player-name');
                    return el && el.innerText === '\u2014';
                }""",
                timeout=20000,
            )
            # Player cards must NOT be filled with guessed engine names.
            assert top_name.inner_text() == "—"
            assert (
                page.locator(".player-card.bottom .player-name").inner_text()
                == "—"
            )
            body_text = page.locator("body").inner_text()
            assert "color assignment unavailable" in body_text.lower(), (
                f"expected the fail-closed note, got {body_text[:200]!r}"
            )
            # The engine names exist only in the badges, not the cards.
            cards = " ".join(page.locator(".player-card").all_inner_texts())
            assert "EngineA" not in cards and "EngineB" not in cards
            assert not console_errors, f"browser console errors: {console_errors}"
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


LIVE_FEN_A = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
LIVE_FEN_B = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"
LIVE_FEN_C = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"


def _live_stream_lines(moves, engine_name, engine_index, cp, depth, pv, last):
    """One position cycle of a cutechess -debug stream (game 1, A white).
    The position is always built from LIVE_FEN_A plus the played moves."""
    position = (
        f"position fen {LIVE_FEN_A} moves {moves}" if moves
        else f"position fen {LIVE_FEN_A}"
    )
    return "\n".join(
        [
            f">{engine_name}({engine_index}): {position}",
            f">{engine_name}({engine_index}): go wtime 180000 btime 180000 winc 2000 binc 2000",
            f"<{engine_name}({engine_index}): info depth {depth} score cp {cp} nodes 100 nps 200 time 3 pv {pv}",
            f"<{engine_name}({engine_index}): bestmove {last}",
        ]
    )


def test_browser_live_stockfish_eval_bar(
    settings, engine_factory, registered
):
    """P4.11 commit 3: the Live page runs the SAME browser Stockfish core on
    the REAL telemetry current_fen — one search per FEN across polls, eval
    bar from Stockfish only, and stale old-FEN output never lands on the new
    FEN.  A fake worker makes every score deterministic: FEN_A (white to
    move) -> +1.00, FEN_B (black to move) -> -1.00, FEN_C -> +1.00."""
    import json

    manifest = json.loads(
        (registered["build_dir"] / "manifest.json").read_text(encoding="utf-8")
    )
    opening_manifest = json.loads(
        (registered["opening_dir"] / "manifest.json").read_text(encoding="utf-8")
    )

    from chessarena.models import RUNNING, PairJob, Tournament, utcnow

    with engine_factory() as session:
        tournament = Tournament(
            id=str(uuid.uuid4()),
            name="live-stockfish-e2e",
            status=RUNNING,
            engine_a_build_id=manifest["build_id"],
            engine_a_profile="current-final",
            engine_b_build_id=manifest["build_id"],
            engine_b_profile="current",
            opening_set_id=opening_manifest["opening_set_id"],
            time_control="blitz_3_2",
            requested_pairs=1,
            config_snapshot={
                "engine_a": {"display_name": "EngineA", "build_id": manifest["build_id"]},
                "engine_b": {"display_name": "EngineB", "build_id": manifest["build_id"]},
                "opening_set": {"opening_set_id": opening_manifest["opening_set_id"]},
                "time_control": "blitz_3_2",
            },
        )
        session.add(tournament)
        session.flush()
        tournament.started_at = utcnow()
        pair = PairJob(
            id=str(uuid.uuid4()),
            tournament_id=tournament.id,
            pair_index=0,
            opening_index=0,
            status=RUNNING,
        )
        session.add(pair)
        session.flush()
        run_dir = settings.run_root / tournament.id / "pairs" / "000000" / "attempt-01"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "opening.epd").write_text(LIVE_FEN_A + "\n", encoding="utf-8")
        stdout = run_dir / "stdout.log"
        stdout.write_text(
            "Started game 1 of 2 (EngineA vs EngineB)\n"
            + _live_stream_lines("", "EngineA", 0, 45, 12, "e2e4 e7e5", "e2e4")
            + "\n",
            encoding="utf-8",
        )
        pair.run_directory = str(run_dir)
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
        _wait_until_up(f"{base}/live?tournament_id={tid}")

        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            console_errors: list[str] = []
            page.on("pageerror", lambda exc: console_errors.append(str(exc)))
            page.route(
                "**/stockfish.wasm.js",
                lambda route: route.fulfill(
                    status=200,
                    content_type="text/javascript",
                    body=FAKE_STALE_OUTPUT_WORKER,
                ),
            )
            page.add_init_script(
                """
                window.__goCount = 0;
                const __origPost = Worker.prototype.postMessage;
                Worker.prototype.postMessage = function (msg) {
                  if (typeof msg === 'string' && msg.startsWith('go ')) {
                    window.__goCount += 1;
                  }
                  return __origPost.apply(this, arguments);
                };
                """
            )
            page.goto(f"{base}/live?tournament_id={tid}", wait_until="domcontentloaded")

            # A. The browser Stockfish panel + eval bar appear on the REAL
            # telemetry FEN and produce a deterministic score.
            panel = page.locator(".analysis-panel")
            panel.wait_for(state="visible", timeout=30000)
            page.wait_for_function(
                """() => {
                    const el = document.querySelector('.analysis-score');
                    return el && el.innerText.includes('1.00');
                }""",
                timeout=30000,
            )
            engine_label = page.locator(".analysis-engine").inner_text()
            assert __import__("re").match(
                r"^Stockfish \d{4}-\d{2}-\d{2} · browser", engine_label
            ), f"expected versioned browser label, got {engine_label!r}"
            assert page.locator(".eval-bar").count() == 1
            assert page.locator(".board-wrap").get_attribute("data-fen") == LIVE_FEN_A
            assert page.evaluate("window.__goCount") == 1, (
                f"expected 1 go for the first FEN, got {page.evaluate('window.__goCount')}"
            )

            # B. The same FEN across live polling cycles must NOT restart the
            # search (poll is ~1.5s; wait covers > 2 cycles).
            page.wait_for_timeout(4000)
            assert page.evaluate("window.__goCount") == 1, (
                "no re-analysis per poll allowed: go count grew on an "
                f"unchanged FEN -> {page.evaluate('window.__goCount')}"
            )

            # C. The stream advances A -> B: the board follows, exactly one
            # new search starts, the panel switches to B's score, and the
            # fake engine's stale +5.00 must never appear.
            with open(stdout, "a", encoding="utf-8") as fh:
                fh.write(_live_stream_lines("e2e4", "EngineB", 1, -30, 13,
                                            "e7e5 g1f3", "e7e5") + "\n")
            page.wait_for_function(
                """() => {
                    const b = document.querySelector('.board-wrap');
                    return b && b.getAttribute('data-fen') === 'rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1';
                }""",
                timeout=20000,
            )
            page.wait_for_function(
                """() => {
                    const el = document.querySelector('.analysis-score');
                    return el && el.innerText.includes('-1.00');
                }""",
                timeout=30000,
            )
            assert page.evaluate("window.__goCount") == 2, (
                f"expected 2 go after A -> B, got {page.evaluate('window.__goCount')}"
            )

            # D. Fast A -> B -> C: the panel/board settle on C's +1.00, no
            # stale A/B score and no fake stale +5.00.
            with open(stdout, "a", encoding="utf-8") as fh:
                fh.write(_live_stream_lines("e2e4 e7e5", "EngineA", 0, 60, 14,
                                            "g1f3 g8f6", "g1f3") + "\n")
            page.wait_for_function(
                """() => {
                    const b = document.querySelector('.board-wrap');
                    return b && b.getAttribute('data-fen') === 'rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2';
                }""",
                timeout=20000,
            )
            page.wait_for_function(
                """() => {
                    const el = document.querySelector('.analysis-score');
                    return el && el.innerText.includes('1.00');
                }""",
                timeout=30000,
            )
            assert page.evaluate("window.__goCount") == 3, (
                f"expected 3 go after A -> B -> C, got {page.evaluate('window.__goCount')}"
            )
            score_text = page.locator(".analysis-score").inner_text()
            assert score_text.startswith("+1.00"), (
                f"C must show the fresh +1.00, got {score_text!r}"
            )
            assert "5.00" not in score_text, f"stale output leaked: {score_text!r}"
            # Engine self-evals remain independent of the browser Stockfish.
            assert page.locator(".live-engine").count() == 2
            assert not console_errors, f"browser console errors: {console_errors}"
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_browser_live_dispose_on_completed(
    settings, engine_factory, registered
):
    """P4.11 commit 3 repair: a Live match flipping live -> COMPLETED must
    terminate the browser Stockfish session (no orphaned 'go infinite'
    burning a CPU core), and re-entering a Live match starts a fresh worker
    and produces a score again."""
    import json

    manifest = json.loads(
        (registered["build_dir"] / "manifest.json").read_text(encoding="utf-8")
    )
    opening_manifest = json.loads(
        (registered["opening_dir"] / "manifest.json").read_text(encoding="utf-8")
    )

    from chessarena.models import COMPLETED, RUNNING, PairJob, Tournament, utcnow

    def _make_running(name):
        with engine_factory() as session:
            tournament = Tournament(
                id=str(uuid.uuid4()),
                name=name,
                status=RUNNING,
                engine_a_build_id=manifest["build_id"],
                engine_a_profile="current-final",
                engine_b_build_id=manifest["build_id"],
                engine_b_profile="current",
                opening_set_id=opening_manifest["opening_set_id"],
                time_control="blitz_3_2",
                requested_pairs=1,
                config_snapshot={
                    "engine_a": {"display_name": "EngineA", "build_id": manifest["build_id"]},
                    "engine_b": {"display_name": "EngineB", "build_id": manifest["build_id"]},
                    "opening_set": {"opening_set_id": opening_manifest["opening_set_id"]},
                    "time_control": "blitz_3_2",
                },
            )
            session.add(tournament)
            session.flush()
            tournament.started_at = utcnow()
            pair = PairJob(
                id=str(uuid.uuid4()),
                tournament_id=tournament.id,
                pair_index=0,
                opening_index=0,
                status=RUNNING,
            )
            session.add(pair)
            session.flush()
            run_dir = (
                settings.run_root / tournament.id / "pairs" / "000000" / "attempt-01"
            )
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "opening.epd").write_text(LIVE_FEN_A + "\n", encoding="utf-8")
            (run_dir / "stdout.log").write_text(
                "Started game 1 of 2 (EngineA vs EngineB)\n"
                + _live_stream_lines("", "EngineA", 0, 45, 12, "e2e4 e7e5", "e2e4")
                + "\n",
                encoding="utf-8",
            )
            pair.run_directory = str(run_dir)
            session.commit()
            return tournament.id

    tid1 = _make_running("live-dispose-a")
    tid2 = _make_running("live-dispose-b")

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
        _wait_until_up(f"{base}/live?tournament_id={tid1}")

        from chessarena.models import Tournament as _Tournament
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            console_errors: list[str] = []
            page.on("pageerror", lambda exc: console_errors.append(str(exc)))
            page.route(
                "**/stockfish.wasm.js",
                lambda route: route.fulfill(
                    status=200,
                    content_type="text/javascript",
                    body=FAKE_STALE_OUTPUT_WORKER,
                ),
            )
            page.add_init_script(
                """
                window.__goCount = 0;
                window.__terminateCount = 0;
                const __origPost = Worker.prototype.postMessage;
                Worker.prototype.postMessage = function (msg) {
                  if (typeof msg === 'string' && msg.startsWith('go ')) {
                    window.__goCount += 1;
                  }
                  return __origPost.apply(this, arguments);
                };
                const __origTerminate = Worker.prototype.terminate;
                Worker.prototype.terminate = function () {
                  window.__terminateCount += 1;
                  return __origTerminate.apply(this, arguments);
                };
                """
            )
            page.goto(f"{base}/live?tournament_id={tid1}", wait_until="domcontentloaded")
            panel = page.locator(".analysis-panel")
            panel.wait_for(state="visible", timeout=30000)
            page.wait_for_function(
                """() => {
                    const el = document.querySelector('.analysis-score');
                    return el && el.innerText.includes('1.00');
                }""",
                timeout=30000,
            )
            assert page.evaluate("window.__goCount") == 1
            assert page.evaluate("window.__terminateCount") == 0, (
                "worker must still be alive while the match is live"
            )

            # The match flips to COMPLETED: the next poll shows the finished
            # state and the browser Stockfish session must be disposed.
            with engine_factory() as session:
                t = session.query(_Tournament).filter(
                    _Tournament.id == tid1
                ).one()
                t.status = COMPLETED
                session.commit()

            page.wait_for_function(
                """() => window.__terminateCount === 1""",
                timeout=20000,
            )
            assert "finished" in page.locator("body").inner_text().lower(), (
                "page must show the completed state"
            )
            assert not console_errors, f"browser console errors: {console_errors}"

            # Re-entering a Live match starts a fresh worker session and
            # produces a score again (no stuck refs from the old session).
            page.goto(f"{base}/live?tournament_id={tid2}", wait_until="domcontentloaded")
            panel.wait_for(state="visible", timeout=30000)
            page.wait_for_function(
                """() => {
                    const el = document.querySelector('.analysis-score');
                    return el && el.innerText.includes('1.00');
                }""",
                timeout=30000,
            )
            assert page.evaluate("window.__goCount") == 1, (
                "re-entered session must start exactly one search"
            )
            assert page.evaluate("window.__terminateCount") == 0, (
                "the fresh session's worker must be alive"
            )
            assert not console_errors, f"browser console errors: {console_errors}"
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_browser_live_stockfish_failure_isolated(
    settings, engine_factory, registered
):
    """P4.11 commit 3 (E): a browser Stockfish worker failure shows
    'Stockfish unavailable' while the live telemetry (board, sides, clocks,
    engine self-evals/PV) keeps working."""
    import json

    manifest = json.loads(
        (registered["build_dir"] / "manifest.json").read_text(encoding="utf-8")
    )
    opening_manifest = json.loads(
        (registered["opening_dir"] / "manifest.json").read_text(encoding="utf-8")
    )

    from chessarena.models import RUNNING, PairJob, Tournament, utcnow

    with engine_factory() as session:
        tournament = Tournament(
            id=str(uuid.uuid4()),
            name="live-stockfish-fail-e2e",
            status=RUNNING,
            engine_a_build_id=manifest["build_id"],
            engine_a_profile="current-final",
            engine_b_build_id=manifest["build_id"],
            engine_b_profile="current",
            opening_set_id=opening_manifest["opening_set_id"],
            time_control="blitz_3_2",
            requested_pairs=1,
            config_snapshot={
                "engine_a": {"display_name": "EngineA", "build_id": manifest["build_id"]},
                "engine_b": {"display_name": "EngineB", "build_id": manifest["build_id"]},
                "opening_set": {"opening_set_id": opening_manifest["opening_set_id"]},
                "time_control": "blitz_3_2",
            },
        )
        session.add(tournament)
        session.flush()
        tournament.started_at = utcnow()
        pair = PairJob(
            id=str(uuid.uuid4()),
            tournament_id=tournament.id,
            pair_index=0,
            opening_index=0,
            status=RUNNING,
        )
        session.add(pair)
        session.flush()
        run_dir = settings.run_root / tournament.id / "pairs" / "000000" / "attempt-01"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "opening.epd").write_text(LIVE_FEN_A + "\n", encoding="utf-8")
        (run_dir / "stdout.log").write_text(
            "Started game 1 of 2 (EngineA vs EngineB)\n"
            + _live_stream_lines("", "EngineA", 0, 45, 12, "e2e4 e7e5", "e2e4")
            + "\n",
            encoding="utf-8",
        )
        pair.run_directory = str(run_dir)
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
        _wait_until_up(f"{base}/live?tournament_id={tid}")

        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            console_errors: list[str] = []
            page.on("pageerror", lambda exc: console_errors.append(str(exc)))
            page.route("**/stockfish.wasm.js", lambda route: route.abort())
            page.goto(f"{base}/live?tournament_id={tid}", wait_until="domcontentloaded")

            # Worker failure -> visible unavailable state in the Stockfish
            # panel only.
            err_panel = page.locator(".analysis-panel")
            err_panel.wait_for(state="visible", timeout=20000)
            page.wait_for_function(
                """() => {
                    const el = document.querySelector('.analysis-panel');
                    return el && el.innerText.includes('unavailable');
                }""",
                timeout=25000,
            )
            # Live telemetry is untouched: real board, sides, self-evals, PV.
            page.wait_for_function(
                """() => {
                    const b = document.querySelector('.board-wrap');
                    return b && b.getAttribute('data-fen') !== null;
                }""",
                timeout=20000,
            )
            assert page.locator(".board-wrap").get_attribute("data-fen") == LIVE_FEN_A
            assert page.locator(".player-card.top .player-name").inner_text() == "EngineB"
            assert page.locator(".player-card.bottom .player-name").inner_text() == "EngineA"
            assert page.locator(".live-engine").count() == 2
            engines = " ".join(page.locator(".live-engine").all_inner_texts())
            assert "0.45" in engines, f"engine self-eval missing: {engines!r}"
            assert page.locator(".live-engine-pv").count() >= 1
            assert page.locator(".live-clock").count() >= 1
            assert not console_errors, f"browser console errors: {console_errors}"
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
