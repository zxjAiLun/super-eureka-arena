"""V2.2-B browser smoke: the formal experiment wizard end-to-end.

Form Experiment -> confirmation -> experimental preset candidate ->
Preview (baseline/SPRT/exclusion/seed) -> Create DRAFT -> detail shows
the Experiment card -> delete. NEVER started (zero CPU).
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

from chessarena.models import EngineBuild, EnginePreset
from chessarena.services import versions

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
    import hashlib
    import json

    manifest = json.loads(
        (registered["build_dir"] / "manifest.json").read_text(encoding="utf-8")
    )
    build_dir = Path(registered["build_dir"]).parent / "cand-build"
    build_dir.mkdir(parents=True, exist_ok=True)
    content = b"browser formal wizard candidate binary"
    (build_dir / "engine").write_bytes(content)
    m2 = {
        "build_id": "cand-build", "git_sha": "b" * 40,
        "binary_sha256": hashlib.sha256(content).hexdigest(),
    }
    with engine_factory() as session:
        session.add(EngineBuild(
            build_id="cand-build", engine_name="Test", git_sha=m2["git_sha"],
            binary_path=str(build_dir / "engine"),
            binary_sha256=m2["binary_sha256"], platform="x86_64",
            supported_profiles=[], manifest=m2, enabled=True,
        ))
        session.add(EnginePreset(
            preset_id="exp-candidate", build_id="cand-build",
            display_name="Experimental Candidate",
            command_args=[], uci_options={}, category="custom",
            public_visible=True, enabled=True,
        ))
        versions.create_version_from_build(
            session, version_id="ce-prod-baseline",
            display_name="Production Baseline",
            build_id=manifest["build_id"], command_args=[], uci_options={},
            status="production", rating_enabled=True, public_visible=True,
        )
        versions.set_channel(session, "current-final", "ce-prod-baseline")
        session.commit()

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
        _wait_until_up(f"{base}/admin/experiments/formal/new")
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


def test_formal_wizard_browser_smoke(site):
    base = site["base"]
    page = site["browser"].new_page()
    errors = []
    statuses = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("response", lambda r: statuses.append(r.status))

    # 1) the wizard form: baseline locked to current production
    page.goto(f"{base}/admin/experiments/formal/new")
    content = page.content()
    assert "not selectable" in content
    assert "Production Baseline" in content
    assert "ce-prod-baseline" in content

    # 2) fill and preview
    page.fill("input[name=experiment_id]", "browser-formal-smoke")
    page.select_option("select[name=experiment_stage]", "confirmation")
    page.fill("textarea[name=experiment_purpose]",
              "Browser smoke formal experiment.")
    page.select_option("select[name=candidate]", "preset:exp-candidate")
    page.fill("input[name=max_pairs]", "8")
    with page.expect_response(
        lambda r: "/formal/preview" in r.url and r.request.method == "POST"
    ) as resp_info:
        page.click("button:has-text('Preview formal experiment')")
    resp = resp_info.value
    assert resp.status == 200, (resp.status, resp.text()[:300])
    page.wait_for_selector("text=Formal experiment preview")

    content = page.content()
    assert "browser-formal-smoke" in content
    assert "Production Baseline" in content  # frozen baseline shown
    assert "Experimental Candidate" in content
    assert "Wald bounds" in content
    assert "Prior exclusion" in content
    assert "New sample" in content
    assert "Create formal experiment DRAFT" in content

    # 3) create the DRAFT
    page.click("button:has-text('Create formal experiment DRAFT')")
    page.wait_for_url("**/admin/tournaments/*")
    page.wait_for_selector("#experiment-panel")
    content = page.content()
    assert "browser-formal-smoke" in content
    assert "Pentanomial SPRT" in content
    # DRAFT, never started
    assert "State" in content

    # 4) delete the DRAFT (confirm dialog), leaving zero residue
    page.on("dialog", lambda d: d.accept())
    page.click("form[action*='delete'] button[type=submit]")
    page.wait_for_url("**/admin/")
    with __import__("sqlite3").connect(
            os.environ["ARENA_DB_URL"].replace("sqlite:///", "")) as con:
        left = con.execute(
            "SELECT COUNT(*) FROM tournaments WHERE name=?",
            ("browser-formal-smoke",)).fetchone()[0]
    assert left == 0

    assert not errors, errors
    assert all(s < 500 for s in statuses), \
        [s for s in statuses if s >= 500]
