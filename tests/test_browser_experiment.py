"""V2.2-A browser smoke: New Match with Experiment Context -> detail
shows Candidate/Baseline + Experiment card. Light by design — no real
pairs are run; only page navigation, form submission, zero page errors.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

pytest.importorskip("playwright")

ARENA_ROOT = Path(__file__).resolve().parents[1]


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


@pytest.fixture()
def site(settings, engine_factory, registered, monkeypatch):
    for key, value in (
        ("ARENA_DB_URL", settings.db_url),
        ("ARENA_RUN_ROOT", str(settings.run_root)),
        ("ARENA_BUILD_ROOT", str(settings.build_root)),
        ("ARENA_OPENING_ROOT", str(settings.opening_root)),
        ("ARENA_CUTECHESS", str(settings.cutechess)),
        ("ARENA_BASE_PATH", settings.base_path),
    ):
        monkeypatch.setenv(key, value)

    port = _free_port()
    monkeypatch.setenv(
        "ARENA_PUBLIC_URL", f"http://127.0.0.1:{port}/chessarena")
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", "chessarena.main:create_app",
            "--factory", "--host", "127.0.0.1", "--port", str(port),
            "--log-level", "warning",
        ],
        cwd=str(ARENA_ROOT), env=dict(os.environ),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}/chessarena"
    try:
        _wait_until_up(f"{base}/admin/tournaments/new")
        from playwright.sync_api import sync_playwright

        pw = sync_playwright().start()
        browser = pw.chromium.launch()
        try:
            yield {"base": base, "browser": browser}
        finally:
            browser.close()
            pw.stop()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_experiment_form_browser_smoke(site):
    base = site["base"]
    page = site["browser"].new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    statuses = []
    page.on("response", lambda r: statuses.append(r.status))

    # New Match page carries the Candidate/Baseline naming
    page.goto(f"{base}/admin/tournaments/new")
    content = page.content()
    assert "Candidate — Engine A" in content
    assert "Baseline — Engine B" in content
    assert "Track this match as an experiment" in content

    # the experiment fields start hidden
    assert page.locator("#experiment-fields").is_hidden()

    # enable the context; fields appear and become required
    page.check("#experiment-enabled")
    assert page.locator("#experiment-fields").is_visible()

    # fill and submit
    page.fill("input[name=name]", "browser-exp-smoke")
    page.select_option("select[name=engine_a_side]",
                       "preset:chessengine-production")
    page.select_option("select[name=engine_b_side]",
                       "preset:chessengine-legacy-current")
    # the test opening set is EPD: clear the PGN-only plies default
    page.fill("input[name=opening_plies]", "")
    page.fill("input[name=experiment_id]", "browser-smoke-exp")
    page.select_option("select[name=experiment_stage]", "screening")
    page.fill("textarea[name=experiment_purpose]",
              "Browser smoke experiment context.")
    with page.expect_response(
        lambda r: "/admin/tournaments" in r.url
        and r.request.method == "POST"
    ) as resp_info:
        page.click("button[type=submit]")
    resp = resp_info.value
    assert resp.status == 303, (resp.status, resp.url, resp.text()[:300])
    page.wait_for_url("**/admin/tournaments/*")

    # the detail page shows the experiment card
    page.wait_for_selector("#experiment-panel")
    content = page.content()
    assert "browser-smoke-exp" in content
    assert "Browser smoke experiment context." in content
    assert "screening" in content
    # candidate/baseline labels from the frozen snapshot
    assert "ChessEngine Production" in content
    assert "ChessEngine Legacy Baseline" in content
    # fixed-pair run: explicit no-decision wording, never PASS/FAIL
    assert "Fixed-pair measurement" in content
    assert "No formal decision" in content
    assert "PASS" not in content and "FAIL" not in content

    # the live status fragment route is reachable (pure read)
    r = page.request.get(
        page.url.rstrip("/") + "/experiment-status")
    assert r.status == 200

    # P1 regression: let REAL htmx polling run through at least TWO
    # refresh cycles and prove there is no panel nesting and no polling
    # URL drift (the old bug nested a poller inside the panel and then
    # requested /tournaments//experiment-status).
    fragment_urls = []

    def _collect(response):
        if "/experiment-status" in response.url:
            fragment_urls.append((response.url, response.status))

    page.on("response", _collect)
    page.wait_for_timeout(12000)  # >= 2 polling cycles at every 5s
    panel_count = page.locator("#experiment-panel").count()
    assert panel_count == 1, (
        f"experiment panel nested: {panel_count} panels after polling")
    assert len(fragment_urls) >= 2, fragment_urls
    for url, status_code in fragment_urls:
        assert status_code < 500, (url, status_code)
        assert "//experiment-status" not in url.replace(
            "://", ""), f"polling URL drifted: {url}"
        assert "/tournaments//experiment-status" not in url, url

    assert not errors, errors
    assert all(s < 500 for s in statuses), \
        [s for s in statuses if s >= 500]
