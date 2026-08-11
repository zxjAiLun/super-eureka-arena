"""S4.3E Phase 1: EngineVersion admin list/detail pages (read-only)."""

from __future__ import annotations

import json

from chessarena.models import COMPLETED, EngineBuild, Tournament, utcnow
from chessarena.services import versions


def _register_build(engine_factory, build_id="20260811-26604c4-linux-x86_64",
                    git_sha="26604c425625d69e5b7e7b967db8926f4da01b8a"):
    with engine_factory() as session:
        session.add(EngineBuild(
            build_id=build_id,
            engine_name="ChessEngineDemo",
            git_sha=git_sha,
            binary_path="/unused/engine",
            binary_sha256="f0e8f91a3a0828a158672cecdf7859dbd9a3c9bac36b965bdcc90db31b51189d",
            platform="linux-x86_64",
            supported_profiles=["current-final", "current"],
            manifest={"build_id": build_id, "git_sha": git_sha},
            enabled=True,
        ))
        session.commit()


def test_admin_versions_list(app_client, engine_factory, registered):
    _register_build(engine_factory)
    with engine_factory() as session:
        versions.create_version_from_build(
            session, version_id="ce-test-20260811",
            display_name="CurrentFinal · Test · 2026-08-11",
            build_id="20260811-26604c4-linux-x86_64",
            status="production",
        )
        versions.set_channel(session, "current-final", "ce-test-20260811")
    r = app_client.get("/chessarena/admin/versions/")
    assert r.status_code == 200
    body = r.text
    assert "ce-test-20260811" in body
    assert "CurrentFinal · Test · 2026-08-11" in body
    assert "current-final" in body
    assert "20260811-26604c4" in body
    assert "26604c4256" in body  # source SHA short
    assert "f0e8f91a3a08" in body  # binary SHA short
    assert "production" in body
    # UTC+8 display for the created timestamp.
    assert "UTC+8" in body


def test_admin_version_detail(app_client, engine_factory, tournament_factory,
                              registered):
    _register_build(engine_factory)
    with engine_factory() as session:
        versions.create_version_from_build(
            session, version_id="ce-test-20260811",
            display_name="CurrentFinal · Test · 2026-08-11",
            build_id="20260811-26604c4-linux-x86_64",
            status="production",
        )
        versions.set_channel(session, "current-final", "ce-test-20260811")
        tid = tournament_factory(name="version-match", pairs=1,
                                 time_control="blitz_3_2", status=COMPLETED)
        t = session.query(Tournament).filter(Tournament.id == tid).one()
        t.completed_pairs = 1
        t.finished_at = utcnow()
        snap = dict(t.config_snapshot or {})
        snap["engine_a"] = {
            "version_id": "ce-test-20260811",
            "display_name": "CurrentFinal · Test · 2026-08-11",
            "build_id": "20260811-26604c4-linux-x86_64",
            "command_args": [],
            "uci_options": {},
            "git_sha": "26604c425625d69e5b7e7b967db8926f4da01b8a",
            "binary_sha256": "f0e8f91a3a0828a158672cecdf7859dbd9a3c9bac36b965bdcc90db31b51189d",
        }
        t.config_snapshot = snap
        session.commit()
    r = app_client.get("/chessarena/admin/versions/ce-test-20260811")
    assert r.status_code == 200
    body = r.text
    # Immutable identity displayed in full.
    assert "Identity fingerprint" in body
    assert "f0e8f91a3a0828a158672cecdf7859dbd9a3c9bac36b965bdcc90db31b51189d" in body
    assert "26604c425625d69e5b7e7b967db8926f4da01b8a" in body
    assert "current-final" in body
    # Current ratings by TC (the version is a public participant: 1800 initial).
    assert "Current ratings by time control" in body
    assert "3+2" in body and "1800" in body
    # Match history includes the match whose snapshot carries this version.
    assert "version-match" in body
    # No edit surface for immutable fields.
    assert 'action="' not in body.replace(
        f"/admin/versions/ce-test-20260811", ""
    ) or "method=\"post\"" not in body


def test_admin_version_detail_matches_legacy_fingerprint(
        app_client, engine_factory, tournament_factory, registered):
    """P2: a legacy snapshot WITHOUT version_id whose frozen fingerprint
    uniquely matches the EngineVersion must be listed in its match history —
    exactly like it counts toward the version's Elo/Games/W-D-L."""
    _register_build(engine_factory)
    with engine_factory() as session:
        v = versions.create_version_from_build(
            session, version_id="ce-test-20260811",
            display_name="CurrentFinal · Test · 2026-08-11",
            build_id="20260811-26604c4-linux-x86_64",
            status="production",
        )
        fingerprint = v.identity_fingerprint
        tid = tournament_factory(name="legacy-fp-match", pairs=1,
                                 time_control="blitz_3_2", status=COMPLETED)
        t = session.query(Tournament).filter(Tournament.id == tid).one()
        t.completed_pairs = 1
        t.finished_at = utcnow()
        snap = dict(t.config_snapshot or {})
        # NO version_id: the frozen identity must resolve via fingerprint.
        snap["engine_a"] = {
            "preset_id": "chessengine-production",
            "display_name": "CurrentFinal",
            "build_id": "20260811-26604c4-linux-x86_64",
            "command_args": [],
            "uci_options": {},
            "git_sha": "26604c425625d69e5b7e7b967db8926f4da01b8a",
            "binary_sha256": "f0e8f91a3a0828a158672cecdf7859dbd9a3c9bac36b965bdcc90db31b51189d",
        }
        t.config_snapshot = snap
        session.commit()
        assert fingerprint == versions.identity_fingerprint(
            "f0e8f91a3a0828a158672cecdf7859dbd9a3c9bac36b965bdcc90db31b51189d",
            [], {},
        )
    r = app_client.get("/chessarena/admin/versions/ce-test-20260811")
    assert r.status_code == 200
    body = r.text
    assert "legacy-fp-match" in body, (
        "legacy fingerprint-matched matches must appear in the version's "
        "match history"
    )


def test_admin_version_detail_404(app_client, engine_factory):
    r = app_client.get("/chessarena/admin/versions/not-a-version")
    assert r.status_code == 404
