"""S4.3E Phase 1 UI integration tests: EngineVersion side selection in the
match form and the version catalog on the public ratings page."""

from __future__ import annotations

import json

from chessarena.services import versions


def _make_version(engine_factory, registered, version_id="ce-ui-v1",
                   display_name="UI Version", command_args=None):
    manifest = json.loads(
        (registered["build_dir"] / "manifest.json").read_text(encoding="utf-8")
    )
    with engine_factory() as session:
        versions.create_version_from_build(
            session,
            version_id=version_id,
            display_name=display_name,
            build_id=manifest["build_id"],
            command_args=command_args or [],
            status="production",
        )
    return manifest


def test_new_match_form_lists_versions(app_client, engine_factory, registered):
    _make_version(engine_factory, registered)
    page = app_client.get("/chessarena/admin/tournaments/new")
    assert page.status_code == 200
    html = page.text
    assert "ce-ui-v1" in html
    assert "EngineVersions (stable rated identity)" in html


def test_admin_form_creates_version_tournament(app_client, engine_factory,
                                               registered):
    manifest = _make_version(engine_factory, registered)
    opening = app_client.get("/chessarena/api/v1/opening-sets").json()[0]
    page = app_client.get("/chessarena/admin/tournaments/new")
    token = app_client.cookies.get("arena_csrf")
    r = app_client.post(
        "/chessarena/admin/tournaments",
        data={
            "_csrf_token": token,
            "name": "version-vs-preset",
            "engine_a_side": "version:ce-ui-v1",
            "engine_b_side": "preset:chessengine-production",
            "opening_set_id": opening["opening_set_id"],
            "time_control": "blitz_3_2",
            "pairs": "2",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text[:300]
    tid = r.headers["location"].rsplit("/", 1)[-1]
    detail = app_client.get(f"/chessarena/admin/tournaments/{tid}")
    assert detail.status_code == 200
    data = app_client.get(f"/chessarena/api/v1/tournaments/{tid}").json()
    snap_a = data["config_snapshot"]["engine_a"]
    assert snap_a["version_id"] == "ce-ui-v1"
    assert snap_a["identity_fingerprint"]
    assert snap_a["build_id"] == manifest["build_id"]
    assert snap_a["command_args"] == []
    assert snap_a["source_sha"] == manifest["git_sha"]
    # preset side stays on the legacy path
    assert "version_id" not in data["config_snapshot"]["engine_b"]


def test_admin_form_version_side_rejects_custom_elo(app_client, engine_factory,
                                                    registered):
    _make_version(engine_factory, registered)
    opening = app_client.get("/chessarena/api/v1/opening-sets").json()[0]
    page = app_client.get("/chessarena/admin/tournaments/new")
    token = app_client.cookies.get("arena_csrf")
    r = app_client.post(
        "/chessarena/admin/tournaments",
        data={
            "_csrf_token": token,
            "name": "version-elo",
            "engine_a_side": "version:ce-ui-v1",
            "engine_a_elo": "1850",
            "engine_b_side": "preset:chessengine-production",
            "opening_set_id": opening["opening_set_id"],
            "time_control": "blitz_3_2",
            "pairs": "2",
        },
        follow_redirects=False,
    )
    # Version sides reject custom_elo: the form path drops the field (fail
    # closed), so the created snapshot carries no Elo override.
    assert r.status_code == 303, r.text[:300]
    tid = r.headers["location"].rsplit("/", 1)[-1]
    data = app_client.get(f"/chessarena/api/v1/tournaments/{tid}").json()
    snap_a = data["config_snapshot"]["engine_a"]
    assert snap_a["version_id"] == "ce-ui-v1"
    assert "custom_elo" not in snap_a
    assert "UCI_Elo" not in snap_a["uci_options"]


def test_public_ratings_shows_version_catalog(app_client, engine_factory,
                                              registered):
    _make_version(engine_factory, registered, version_id="ce-ui-ratings",
                  display_name="Ratings Version")
    with engine_factory() as session:
        versions.set_channel(session, "current-final", "ce-ui-ratings")
    page = app_client.get("/chessarena/ratings/")
    assert page.status_code == 200
    html = page.text
    assert "EngineVersions" in html
    assert "Ratings Version" in html
    assert "ce-ui-ratings" in html
    assert "current-final" in html
