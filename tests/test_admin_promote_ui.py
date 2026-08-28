"""V2.1-B2: guarded promotion confirmation UI + EngineVersion timeline.

The UI is only a shell over plan_channel_promotion() /
promote_channel(): the preview never mutates, blocked plans show no
confirm, and the POST re-runs the full production gate at submission
time (GET->POST registry drift must fail closed).
"""

from __future__ import annotations

from chessarena.models import EngineBuild, EngineVersion
from chessarena.services import versions


def _scene(engine_factory, registered):
    """Old production on the channel + a default-identity candidate on a
    second build. Returns (manifest, old_id, target_id)."""
    import json
    manifest = json.loads(
        (registered["build_dir"] / "manifest.json").read_text(encoding="utf-8")
    )
    import hashlib
    from pathlib import Path
    build_dir = Path(registered["build_dir"]).parent / "build2"
    build_dir.mkdir(parents=True, exist_ok=True)
    content = b"second dummy engine binary for promote ui tests"
    (build_dir / "engine").write_bytes(content)
    m2 = {
        "build_id": "build2-x86_64",
        "git_sha": "b" * 40,
        "binary_sha256": hashlib.sha256(content).hexdigest(),
    }
    with engine_factory() as session:
        if session.query(EngineBuild).filter(
                EngineBuild.build_id == "build2-x86_64").first() is None:
            session.add(EngineBuild(
                build_id="build2-x86_64", engine_name="Test",
                git_sha=m2["git_sha"],
                binary_path=str(build_dir / "engine"),
                binary_sha256=m2["binary_sha256"], platform="x86_64",
                supported_profiles=[], manifest=m2, enabled=True,
            ))
        versions.create_version_from_build(
            session, version_id="ce-old-prod", display_name="Old Prod",
            build_id=manifest["build_id"], command_args=[],
            status="production", rating_enabled=True, public_visible=True,
        )
        versions.set_channel(session, "current-final", "ce-old-prod")
        versions.create_version_from_build(
            session, version_id="ce-target", display_name="Target",
            build_id="build2-x86_64", command_args=[], uci_options={},
            status="candidate",
        )
        session.commit()
    return manifest, "ce-old-prod", "ce-target"


def _csrf(app_client):
    app_client.get("/chessarena/admin/versions/")  # establish cookie
    return app_client.cookies.get("arena_csrf")


# ---------------------------------------------------------------------------
# (6) preview is zero-mutation and renders plan errors/impact faithfully
# ---------------------------------------------------------------------------
def test_preview_zero_mutation_renders_plan(app_client, engine_factory,
                                             registered):
    manifest, old_id, target_id = _scene(engine_factory, registered)
    r = app_client.get(
        f"/chessarena/admin/versions/{target_id}/promote/current-final")
    assert r.status_code == 200
    body = r.text
    # full plan view
    assert "Current production" in body or "Current" in body
    assert "ce-old-prod" in body
    assert "ce-target" in body
    assert "historical" in body
    assert "production" in body
    assert "Rated history matches for target: 0" in body
    assert "Confirm promotion" in body
    # ZERO mutation
    with engine_factory() as session:
        assert versions.get_version(
            session, old_id).status == "production"
        assert versions.get_version(
            session, target_id).status == "candidate"
        assert versions.get_channel(
            session, "current-final").engine_version_id == old_id


# ---------------------------------------------------------------------------
# (7) blocked plan page shows errors and NO confirm
# ---------------------------------------------------------------------------
def test_blocked_plan_has_no_confirm(app_client, engine_factory, registered):
    manifest, old_id, target_id = _scene(engine_factory, registered)
    # profile-args candidate: the launch-identity gate blocks it
    with engine_factory() as session:
        versions.create_version_from_build(
            session, version_id="ce-profile-cand",
            display_name="Profile Cand",
            build_id=manifest["build_id"],
            command_args=["--profile", "current-final"],
            status="candidate",
        )
        session.commit()
    r = app_client.get(
        "/chessarena/admin/versions/ce-profile-cand/promote/current-final")
    assert r.status_code == 200
    assert "Blocked" in r.text
    assert "command_args=[]" in r.text  # the gate error is rendered
    assert "Confirm promotion" not in r.text

    # a disabled build also blocks the real target
    with engine_factory() as session:
        build = session.query(EngineBuild).filter(
            EngineBuild.build_id == "build2-x86_64").one()
        build.enabled = False
        session.commit()
    r = app_client.get(
        f"/chessarena/admin/versions/{target_id}/promote/current-final")
    assert "disabled" in r.text
    assert "Confirm promotion" not in r.text


# ---------------------------------------------------------------------------
# (8) POST re-runs the gate: build disabled after the GET -> POST fails,
#     channel zero drift
# ---------------------------------------------------------------------------
def test_post_reruns_gate_after_get(app_client, engine_factory, registered):
    manifest, old_id, target_id = _scene(engine_factory, registered)
    token = _csrf(app_client)
    # GET the clean preview (plan ok, confirm visible)
    r = app_client.get(
        f"/chessarena/admin/versions/{target_id}/promote/current-final")
    assert "Confirm promotion" in r.text
    # ... then the registry changes between GET and POST
    with engine_factory() as session:
        build = session.query(EngineBuild).filter(
            EngineBuild.build_id == "build2-x86_64").one()
        build.enabled = False
        session.commit()
    r = app_client.post(
        f"/chessarena/admin/versions/{target_id}/promote/current-final",
        data={"_csrf_token": token},
        follow_redirects=False,
    )
    assert r.status_code == 422  # gate re-ran and failed
    with engine_factory() as session:
        assert versions.get_channel(
            session, "current-final").engine_version_id == old_id
        assert versions.get_version(
            session, old_id).status == "production"
        assert versions.get_version(
            session, target_id).status == "candidate"


# ---------------------------------------------------------------------------
# (9) successful confirm: full atomic transition, immutable fields intact
# ---------------------------------------------------------------------------
def test_confirm_full_transition(app_client, engine_factory, registered):
    manifest, old_id, target_id = _scene(engine_factory, registered)
    token = _csrf(app_client)
    with engine_factory() as session:
        before = {
            "old": {c: getattr(versions.get_version(session, old_id), c)
                    for c in ("version_id", "display_name", "build_id",
                              "command_args", "uci_options", "source_sha",
                              "binary_sha256", "identity_fingerprint")},
            "target": {c: getattr(versions.get_version(session, target_id), c)
                       for c in ("version_id", "display_name", "build_id",
                                 "command_args", "uci_options", "source_sha",
                                 "binary_sha256", "identity_fingerprint")},
        }
    r = app_client.post(
        f"/chessarena/admin/versions/{target_id}/promote/current-final",
        data={"_csrf_token": token},
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert r.headers["location"].endswith(
        f"/admin/versions/{target_id}?promoted=current-final")
    with engine_factory() as session:
        old = versions.get_version(session, old_id)
        tgt = versions.get_version(session, target_id)
        assert old.status == "historical"
        assert tgt.status == "production"
        assert tgt.public_visible is True
        assert tgt.rating_enabled is True
        assert versions.get_channel(
            session, "current-final").engine_version_id == target_id
        for c, v in before["old"].items():
            assert getattr(old, c) == v
        for c, v in before["target"].items():
            assert getattr(tgt, c) == v
    # detail page renders the promoted banner
    r = app_client.get(
        f"/chessarena/admin/versions/{target_id}?promoted=current-final")
    assert "Promoted to" in r.text


# ---------------------------------------------------------------------------
# (10) timeline: production/current + history in order, channel badge only
#      on the real target, no-artifact note present
# ---------------------------------------------------------------------------
def test_timeline_renders_lineage(app_client, engine_factory, registered):
    manifest, old_id, target_id = _scene(engine_factory, registered)
    token = _csrf(app_client)
    # promote to build a 3-entry lineage: 0806-style old-old, 0811 old, new
    with engine_factory() as session:
        # an OLDER historical version for ordering
        import hashlib
        from pathlib import Path
        build_dir = Path(registered["build_dir"]).parent / "build3"
        build_dir.mkdir(parents=True, exist_ok=True)
        content = b"third dummy engine binary for timeline test"
        (build_dir / "engine").write_bytes(content)
        m3 = {
            "build_id": "build3-x86_64",
            "git_sha": "c" * 40,
            "binary_sha256": hashlib.sha256(content).hexdigest(),
        }
        session.add(EngineBuild(
            build_id="build3-x86_64", engine_name="Test",
            git_sha=m3["git_sha"], binary_path=str(build_dir / "engine"),
            binary_sha256=m3["binary_sha256"], platform="x86_64",
            supported_profiles=[], manifest=m3, enabled=True,
        ))
        session.commit()
        oldest = versions.create_version_from_build(
            session, version_id="ce-oldest", display_name="Oldest",
            build_id="build3-x86_64", command_args=[], uci_options={},
            status="historical", public_visible=True, rating_enabled=True,
        )
        # a fresh candidate that must NOT appear in the lineage History
        # (own build: same build + default identity would collide)
        build_dir4 = Path(registered["build_dir"]).parent / "build4"
        build_dir4.mkdir(parents=True, exist_ok=True)
        content4 = b"fourth dummy engine binary for timeline test"
        (build_dir4 / "engine").write_bytes(content4)
        m4 = {
            "build_id": "build4-x86_64",
            "git_sha": "d" * 40,
            "binary_sha256": hashlib.sha256(content4).hexdigest(),
        }
        session.add(EngineBuild(
            build_id="build4-x86_64", engine_name="Test",
            git_sha=m4["git_sha"], binary_path=str(build_dir4 / "engine"),
            binary_sha256=m4["binary_sha256"], platform="x86_64",
            supported_profiles=[], manifest=m4, enabled=True,
        ))
        session.commit()
        versions.create_version_from_build(
            session, version_id="ce-pending-cand",
            display_name="Pending Candidate",
            build_id="build4-x86_64", command_args=[], uci_options={},
            status="candidate",
        )
    r = app_client.post(
        f"/chessarena/admin/versions/{target_id}/promote/current-final",
        data={"_csrf_token": token},
        follow_redirects=False,
    )
    assert r.status_code == 302
    r = app_client.get("/chessarena/admin/versions/")
    body = r.text
    assert r.status_code == 200
    # current production section with the channel badge
    assert "Current production" in body
    assert "ce-target" in body
    # the no-artifact note is present
    assert "intentionally omitted" in body
    with engine_factory() as session:
        assert versions.get_channel(
            session, "current-final").engine_version_id == target_id

    # Sections in document order: production, History, Pending/Experimental
    prod_pos = body.index("Current production")
    hist_pos = body.index(">History<")
    pend_pos = body.index("Pending / Experimental")
    assert prod_pos < hist_pos < pend_pos

    # Production node lives in its own section only.
    assert "ce-target" in body[prod_pos:hist_pos]
    assert "ce-target" not in body[hist_pos:]

    # History contains ONLY the historical versions, OLDEST FIRST:
    # ce-oldest was created BEFORE ce-old-prod... except both were created
    # in this test with oldest created after old-prod; the scene's
    # ce-old-prod is older. Assert relative order by created_at:
    # ce-old-prod (scene) precedes ce-oldest in the History table.
    history_html = body[hist_pos:pend_pos]
    assert "ce-old-prod" in history_html
    assert "ce-oldest" in history_html
    assert history_html.index("ce-old-prod") < history_html.index("ce-oldest")
    # candidates/experiments never appear in History
    assert "ce-pending-cand" not in history_html

    # Pending / Experimental holds the candidate, newest first
    pending_html = body[pend_pos:]
    assert "ce-pending-cand" in pending_html
    assert "Pending Candidate" in pending_html
