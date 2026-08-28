"""V2.1-B browser smoke: admin lifecycle pages navigate, forms submit,
and nothing 500s. Light by design — the full behavioral contract lives in
the TestClient regressions; this only proves the pages work in a real
browser (nav renders, create form posts, promotion preview renders,
timeline loads).
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

from chessarena.models import EngineBuild
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
def admin_site(settings, engine_factory, registered):
    import json

    manifest = json.loads(
        (registered["build_dir"] / "manifest.json").read_text(encoding="utf-8")
    )
    # a second build so the create form has a "Not versioned" target
    import hashlib
    build_dir = Path(registered["build_dir"]).parent / "build2"
    build_dir.mkdir(parents=True, exist_ok=True)
    content = b"second dummy engine binary for admin browser smoke"
    (build_dir / "engine").write_bytes(content)
    m2 = {
        "build_id": "build2-x86_64",
        "git_sha": "b" * 40,
        "binary_sha256": hashlib.sha256(content).hexdigest(),
    }
    with engine_factory() as session:
        session.add(EngineBuild(
            build_id="build2-x86_64", engine_name="Test",
            git_sha=m2["git_sha"], binary_path=str(build_dir / "engine"),
            binary_sha256=m2["binary_sha256"], platform="x86_64",
            supported_profiles=[], manifest=m2, enabled=True,
        ))
        versions.create_version_from_build(
            session, version_id="ce-old-prod", display_name="Old Prod",
            build_id=manifest["build_id"], command_args=[],
            status="production", rating_enabled=True, public_visible=True,
        )
        versions.set_channel(session, "current-final", "ce-old-prod")
        session.commit()

    for key, value in (
        ("ARENA_DB_URL", settings.db_url),
        ("ARENA_RUN_ROOT", str(settings.run_root)),
        ("ARENA_BUILD_ROOT", str(settings.build_root)),
        ("ARENA_OPENING_ROOT", str(settings.opening_root)),
        ("ARENA_CUTECHESS", str(settings.cutechess)),
        ("ARENA_BASE_PATH", settings.base_path),
    ):
        os.environ[key] = value

    port = _free_port()
    # The create/promote POSTs carry a browser Origin header; the
    # same-origin contract compares against ARENA_PUBLIC_URL, so point it
    # at the live server the browser actually talks to.
    os.environ["ARENA_PUBLIC_URL"] = f"http://127.0.0.1:{port}/chessarena"
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
        _wait_until_up(f"{base}/admin/builds/")
        from playwright.sync_api import sync_playwright

        pw = sync_playwright().start()
        browser = pw.chromium.launch()
        try:
            yield {"base": base, "browser": browser,
                   "engine_factory": engine_factory}
        finally:
            browser.close()
            pw.stop()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_admin_lifecycle_browser_smoke(admin_site):
    base = admin_site["base"]
    browser = admin_site["browser"]
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    responses = []
    page.on("response", lambda r: responses.append(r.status))

    # 1) builds inventory renders with nav
    page.goto(f"{base}/admin/builds/")
    assert page.locator("table.match-table").count() >= 1
    assert "Not versioned" in page.content()

    # 2) create-version form for the unversioned build
    page.click("text=Create Version")
    page.wait_for_selector("form input[name=version_id]")
    page.fill("input[name=version_id]", "ce-browser-smoke")
    page.fill("input[name=display_name]", "Browser Smoke")
    with page.expect_response(lambda r: "/admin/builds/" in r.url and r.request.method == "POST") as resp_info:
        page.click("button:has-text('Create version')")
    resp = resp_info.value
    assert resp.status in (302, 303), (resp.status, resp.url, resp.text()[:300])
    page.wait_for_url("**/admin/versions/ce-browser-smoke*")
    assert "Browser Smoke" in page.content()

    # 3) promotion preview renders (candidate + default identity)
    page.click("text=Promote to current-final")
    page.wait_for_selector("text=After promotion")
    assert page.locator("text=Confirm promotion").count() >= 1

    # 4) timeline renders with the banner and history
    page.goto(f"{base}/admin/versions/")
    assert "Current production" in page.content()
    assert "intentionally omitted" in page.content()

    # no server errors, no page errors
    assert not errors, errors
    assert all(s < 500 for s in responses), \
        [s for s in responses if s >= 500]
