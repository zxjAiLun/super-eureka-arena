"""V2.1-B1: admin build inventory + default-identity version creation UI.

The web surface is the shell of ``engine-version create --build``: the
form only collects version_id / display_name / status, and the launch
identity (command_args=[], uci_options={}) plus the hidden/unrated
lifecycle are forced server-side. No generic version editor exists.
"""

from __future__ import annotations

import json

from chessarena.models import EngineBuild, EngineVersion
from chessarena.services import versions


def _add_build(engine_factory, build_id="20260830-aaaaaaa-linux-x86_64",
               git_sha="a" * 40, binary_sha256="b" * 64, enabled=True):
    with engine_factory() as session:
        session.add(EngineBuild(
            build_id=build_id,
            engine_name="ChessEngineDemo",
            git_sha=git_sha,
            binary_path="/unused/engine",
            binary_sha256=binary_sha256,
            platform="linux-x86_64",
            supported_profiles=[],
            manifest={"build_id": build_id, "git_sha": git_sha},
            enabled=enabled,
        ))
        session.commit()
    return build_id


def _csrf(app_client):
    app_client.get("/chessarena/admin/builds/")  # establish cookie
    return app_client.cookies.get("arena_csrf")


# ---------------------------------------------------------------------------
# (1) inventory shows enabled/disabled + default-identity registration state
# ---------------------------------------------------------------------------
def test_admin_builds_shows_registration_state(app_client, engine_factory,
                                                registered):
    bid_a = _add_build(engine_factory, "20260830-aaaaaaa-linux-x86_64")
    bid_b = _add_build(engine_factory, "20260830-bbbbbbb-linux-x86_64",
                       git_sha="c" * 40, binary_sha256="d" * 64)
    bid_c = _add_build(engine_factory, "20260830-ccccccc-linux-x86_64",
                       git_sha="e" * 40, binary_sha256="f" * 64,
                       enabled=False)
    with engine_factory() as session:
        versions.create_version_from_build(
            session, version_id="ce-registered", display_name="Registered",
            build_id=bid_a, command_args=[], uci_options={},
            status="candidate",
        )
    r = app_client.get("/chessarena/admin/builds/")
    assert r.status_code == 200
    body = r.text
    assert bid_a in body and bid_b in body and bid_c in body
    # registered build links its version and hides the create button
    assert "ce-registered" in body
    assert "View version" in body
    # unregistered enabled build offers creation
    assert "Not versioned" in body
    assert "Create Version" in body
    # disabled build shows its registry state
    assert "disabled" in body


# ---------------------------------------------------------------------------
# (2) a build with an existing default-identity version cannot mint a second
# ---------------------------------------------------------------------------
def test_duplicate_default_identity_blocked(app_client, engine_factory,
                                             registered):
    bid = _add_build(engine_factory)
    with engine_factory() as session:
        versions.create_version_from_build(
            session, version_id="ce-first", display_name="First",
            build_id=bid, command_args=[], uci_options={},
            status="candidate",
        )
    token = _csrf(app_client)
    # the new-version page itself blocks
    r = app_client.get(f"/chessarena/admin/builds/{bid}/version/new")
    assert r.status_code == 200
    assert "already registered" in r.text
    # and the POST fails closed on fingerprint uniqueness
    r = app_client.post(
        f"/chessarena/admin/builds/{bid}/version",
        data={"_csrf_token": token, "version_id": "ce-second",
              "display_name": "Second", "status": "candidate"},
        follow_redirects=False,
    )
    assert r.status_code == 422
    with engine_factory() as session:
        count = session.query(EngineVersion).filter(
            EngineVersion.build_id == bid).count()
        assert count == 1


# ---------------------------------------------------------------------------
# (3) disabled build: page blocked + POST fail-closed
# ---------------------------------------------------------------------------
def test_disabled_build_creation_fail_closed(app_client, engine_factory,
                                              registered):
    bid = _add_build(engine_factory, enabled=False)
    r = app_client.get(f"/chessarena/admin/builds/{bid}/version/new")
    assert r.status_code == 200
    assert "disabled" in r.text
    assert "Create version" not in r.text  # no form
    token = _csrf(app_client)
    r = app_client.post(
        f"/chessarena/admin/builds/{bid}/version",
        data={"_csrf_token": token, "version_id": "ce-x",
              "display_name": "X", "status": "candidate"},
        follow_redirects=False,
    )
    assert r.status_code == 422
    with engine_factory() as session:
        assert session.query(EngineVersion).filter(
            EngineVersion.version_id == "ce-x").first() is None


# ---------------------------------------------------------------------------
# (4) admin create always yields candidate|experimental + hidden + unrated
#     + default launch identity, and redirects to the version detail
# ---------------------------------------------------------------------------
def test_admin_create_defaults_and_redirect(app_client, engine_factory,
                                             registered):
    bid = _add_build(engine_factory)
    token = _csrf(app_client)
    r = app_client.post(
        f"/chessarena/admin/builds/{bid}/version",
        data={"_csrf_token": token,
              "version_id": "ce-web-cand",
              "display_name": "Web Candidate",
              "status": "candidate"},
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert r.headers["location"].endswith("/admin/versions/ce-web-cand")
    with engine_factory() as session:
        v = versions.get_version(session, "ce-web-cand")
        assert v is not None
        assert v.status == "candidate"
        assert v.public_visible is False
        assert v.rating_enabled is False
        assert list(v.command_args) == []
        assert dict(v.uci_options) == {}
        assert v.build_id == bid
        build = session.query(EngineBuild).filter(
            EngineBuild.build_id == bid).one()
        assert v.source_sha == build.git_sha
        assert v.binary_sha256 == build.binary_sha256

    # experimental is the only other allowed status
    bid2 = _add_build(engine_factory, "20260830-eeeeeee-linux-x86_64",
                      git_sha="1" * 40, binary_sha256="2" * 64)
    r = app_client.post(
        f"/chessarena/admin/builds/{bid2}/version",
        data={"_csrf_token": token,
              "version_id": "ce-web-exp",
              "display_name": "Web Exp",
              "status": "experimental"},
        follow_redirects=False,
    )
    assert r.status_code == 302
    with engine_factory() as session:
        assert versions.get_version(
            session, "ce-web-exp").status == "experimental"
    # production/historical are rejected outright
    bid3 = _add_build(engine_factory, "20260830-ddddddd-linux-x86_64",
                      git_sha="3" * 40, binary_sha256="4" * 64)
    for bad in ("production", "historical"):
        r = app_client.post(
            f"/chessarena/admin/builds/{bid3}/version",
            data={"_csrf_token": token,
                  "version_id": f"ce-web-{bad}",
                  "display_name": "Bad",
                  "status": bad},
            follow_redirects=False,
        )
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# (5) CSRF contract unchanged: missing/wrong token -> 403
# ---------------------------------------------------------------------------
def test_admin_create_csrf_required(app_client, engine_factory, registered):
    bid = _add_build(engine_factory)
    _csrf(app_client)
    r = app_client.post(
        f"/chessarena/admin/builds/{bid}/version",
        data={"version_id": "ce-no-csrf", "display_name": "No CSRF",
              "status": "candidate"},
        follow_redirects=False,
    )
    assert r.status_code == 403
    r = app_client.post(
        f"/chessarena/admin/builds/{bid}/version",
        data={"_csrf_token": "wrong", "version_id": "ce-bad-csrf",
              "display_name": "Bad CSRF", "status": "candidate"},
        follow_redirects=False,
    )
    assert r.status_code == 403
    with engine_factory() as session:
        assert versions.get_version(session, "ce-no-csrf") is None
        assert versions.get_version(session, "ce-bad-csrf") is None
