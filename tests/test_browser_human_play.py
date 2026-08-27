"""Browser E2E for Human vs Engine play.

Starts a real uvicorn server with the feature flag ON and the registered
build pointing at the fake legal UCI engine (a subprocess that always
answers a legal bestmove).  A helper thread runs the worker arbitration
(``_worker_step``) in the background so the engine replies like in
production, then Chromium plays a full game through the real UI: lobby,
drag-move, engine reply via polling, resign, in-page review, PGN download.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

pytest.importorskip("playwright")

from chessarena.models import EngineBuild, HumanGame  # noqa: E402

ARENA_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"
FAKE_ENGINE = FIXTURES / "fake_legal_engine.py"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.sockname()[1] if False else s.getsockname()[1]
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


def _use_fake_engine(engine_factory):
    import hashlib

    def sha256_file(p: Path) -> str:
        return hashlib.sha256(p.read_bytes()).hexdigest()

    with engine_factory() as session:
        build = session.query(EngineBuild).first()
        build.binary_path = str(FAKE_ENGINE)
        build.binary_sha256 = sha256_file(FAKE_ENGINE)
        session.commit()
    os.chmod(FAKE_ENGINE, 0o755)


class _MiniWorker:
    """Background thread running the real worker arbitration loop."""

    def __init__(self, settings, engine_factory):
        from chessarena.services.scheduler import Scheduler
        from chessarena.worker import _worker_step

        self.settings = settings
        self.engine_factory = engine_factory
        self._scheduler = Scheduler(settings, engine_factory)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._step = _worker_step

    def _run(self):
        while not self._stop.is_set():
            try:
                self._step(
                    self.settings, self.engine_factory, self._scheduler, None
                )
            except Exception:
                pass
            self._stop.wait(0.1)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=10)


@pytest.fixture()
def hp_e2e(settings, engine_factory, registered, tmp_path):
    """Server + mini-worker + Playwright browser for one human-play session."""
    from dataclasses import replace

    # The limited-strength preset the UI will pick.
    from chessarena.models import EnginePreset

    manifest = json.loads(
        (registered["build_dir"] / "manifest.json").read_text(encoding="utf-8")
    )
    with engine_factory() as session:
        session.add(
            EnginePreset(
                preset_id="stockfish-limited-1800",
                build_id=manifest["build_id"],
                display_name="Stockfish Limited 1800",
                command_args=[],
                uci_options={"UCI_LimitStrength": True, "UCI_Elo": 1800},
                category="external",
                public_visible=True,
                enabled=True,
            )
        )
        session.commit()
    _use_fake_engine(engine_factory)

    port = _free_port()

    hp_settings = replace(
        settings,
        human_play_enabled=True,
        human_play_opponents="preset:stockfish-limited-1800",
        human_play_movetime_ms=150,
        human_play_poll_seconds=0.2,
        # The Origin check compares scheme+netloc against this URL, so it
        # must reflect the ACTUAL test server socket.
        public_url=f"http://127.0.0.1:{port}/chessarena",
    )

    for key, attr in (
        ("ARENA_DB_URL", "db_url"),
        ("ARENA_RUN_ROOT", "run_root"),
        ("ARENA_BUILD_ROOT", "build_root"),
        ("ARENA_OPENING_ROOT", "opening_root"),
        ("ARENA_CUTECHESS", "cutechess"),
        ("ARENA_BASE_PATH", "base_path"),
        ("ARENA_PUBLIC_URL", "public_url"),
    ):
        os.environ[key] = str(getattr(hp_settings, attr))
    os.environ["ARENA_HUMAN_PLAY_ENABLED"] = "true"
    os.environ["ARENA_HUMAN_PLAY_OPPONENTS"] = "preset:stockfish-limited-1800"
    os.environ["ARENA_HUMAN_PLAY_MOVETIME_MS"] = "150"

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

    worker = _MiniWorker(hp_settings, engine_factory)

    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    browser = pw.chromium.launch()
    try:
        _wait_until_up(f"{base}/human-play/")
        worker.start()
        yield {"base": base, "browser": browser, "settings": hp_settings}
    finally:
        worker.stop()
        browser.close()
        pw.stop()
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        os.environ.pop("ARENA_HUMAN_PLAY_ENABLED", None)
        os.environ.pop("ARENA_HUMAN_PLAY_OPPONENTS", None)
        os.environ.pop("ARENA_HUMAN_PLAY_MOVETIME_MS", None)


def _wait_for(page, selector, timeout=15000):
    page.wait_for_selector(selector, timeout=timeout)


def _wait_for_status(page, text, timeout=15000):
    deadline = time.time() + timeout / 1000
    while time.time() < deadline:
        content = page.locator(".hp-status").inner_text()
        if text in content:
            return content
        time.sleep(0.2)
    raise AssertionError(
        f"status never became {text!r}; last: "
        f"{page.locator('.hp-status').inner_text()}"
    )


def test_browser_human_play_full_flow(hp_e2e):
    page = hp_e2e["browser"].new_page()
    console_errors = []

    def _on_console(msg):
        if msg.type == "error":
            console_errors.append(msg.text)

    page.on("console", _on_console)
    page.on("pageerror", lambda e: console_errors.append(str(e)))

    # Lobby: opponent select is populated, start a game.
    page.goto(f"{hp_e2e['base']}/human-play/")
    _wait_for(page, ".hp-lobby select")
    page.select_option(".hp-lobby select >> nth=0", "preset:stockfish-limited-1800")
    page.click(".hp-start")

    # Board appears, human is white, it is our move.
    _wait_for(page, ".hp-status[data-state='your-move']")

    # Play 1. e4 by drag-and-drop.
    page.mouse.move(700, 300)  # placeholder; real squares resolved below

    # Resolve board squares from the SVG geometry: e2 -> e4.
    board = page.locator(".board-wrap").first
    box = board.bounding_box()
    side = box["width"]

    def square_xy(square: str, orientation: str = "white"):
        files = "abcdefgh"
        ranks = "12345678"
        f = files.index(square[0])
        r = ranks.index(square[1])
        if orientation == "white":
            x = box["x"] + (f + 0.5) * side / 8
            y = box["y"] + (7 - r + 0.5) * side / 8
        else:
            x = box["x"] + (7 - f + 0.5) * side / 8
            y = box["y"] + (r + 0.5) * side / 8
        return x, y

    x1, y1 = square_xy("e2")
    x2, y2 = square_xy("e4")
    page.mouse.move(x1, y1)
    page.mouse.down()
    page.mouse.move(x2, y2, steps=5)
    page.mouse.up()

    # The human move is submitted immediately; wait for the move row.
    try:
        _wait_for(page, ".move-row")
    except Exception:
        raise AssertionError(
            "drag move not recorded; status="
            + page.locator(".hp-status").inner_text()
            + "; console="
            + str(console_errors)
        )
    # Engine replies via polling; then it is our move again.
    _wait_for_status(page, "Your move")
    moves = page.locator(".move-row")
    assert moves.count() >= 1

    # Move list shows 1. e4 <engine reply>.
    row_text = moves.first.inner_text()
    assert "e4" in row_text

    # Resign via the button (accept confirm).
    page.on("dialog", lambda d: d.accept())
    page.click(".hp-resign")

    # Terminal: status shows the result, review controls + PGN appear.
    _wait_for(page, ".hp-review-controls")
    _wait_for_status(page, "0–1")
    _wait_for(page, ".hp-link-button")
    fen_before_review = page.locator(".board-wrap").get_attribute("data-fen")
    assert fen_before_review, (
        "board-wrap has no data-fen after resign; DOM: "
        + page.locator(".hp-main").evaluate(
            "(el) => el.outerHTML.slice(0, 500)"
        )
    )

    # In-page review: click the first move SAN, board must go back to e4 FEN.
    page.click(".move-row .move-san >> nth=0")
    deadline = time.time() + 5
    board_data_fen = None
    while time.time() < deadline:
        board_data_fen = page.locator(".board-wrap").get_attribute("data-fen")
        if board_data_fen and board_data_fen != fen_before_review:
            break
        time.sleep(0.2)
    assert board_data_fen and board_data_fen.startswith(
        "rnbqkbnr/pppppppp/8/8/4P3"
    ), f"unexpected review FEN: {board_data_fen!r}"

    # Back to the end.
    page.click(".hp-review-controls button >> nth=3")  # ⏭
    fen_end = page.locator(".board-wrap").get_attribute("data-fen")
    assert fen_end != board_data_fen

    assert console_errors == [], console_errors


def test_browser_human_play_session_restore(hp_e2e, engine_factory):
    page = hp_e2e["browser"].new_page()
    console_msgs = []
    page.on("console", lambda m: console_msgs.append(f"{m.type}: {m.text}"))
    page.on("pageerror", lambda e: console_msgs.append(f"PAGEERROR: {e}"))
    page.goto(f"{hp_e2e['base']}/human-play/")
    _wait_for(page, ".hp-lobby select")
    page.click(".hp-start")
    try:
        _wait_for(page, ".hp-status[data-state='your-move']")
    except Exception:
        body = page.locator("body").inner_text()
        raise AssertionError(
            f"game did not start. body tail: {body[-400:]}; "
            f"console: {console_msgs}"
        )
    # Reload the page: the game must be restored from localStorage, not the
    # lobby (token + game id persisted).
    page.reload()
    _wait_for(page, ".hp-status[data-state='your-move']")
    assert page.locator(".hp-lobby").count() == 0


def test_browser_human_play_flag_off_404(settings, engine_factory, registered):
    """With the flag off the page 404s (deployed-but-disabled dark launch)."""
    port = _free_port()
    env = dict(os.environ)
    env.pop("ARENA_HUMAN_PLAY_ENABLED", None)
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
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}/chessarena"
    try:
        # Wait for the server (any public route works).
        deadline = time.time() + 40
        while time.time() < deadline:
            try:
                urllib.request.urlopen(f"{base}/", timeout=2)
                break
            except urllib.error.HTTPError:
                break
            except Exception:
                time.sleep(0.5)
        req = urllib.request.Request(f"{base}/human-play/")
        try:
            urllib.request.urlopen(req, timeout=5)
            raise AssertionError("expected 404")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
