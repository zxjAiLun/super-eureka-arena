"""S4.3E Phase 1 EngineVersion tests: stable immutable rated-engine identity.

Covers creation (build/preset), immutability, fingerprint uniqueness, legacy
rating-history mapping, version-vs-version tournaments, channels, and the
minimal API.
"""

from __future__ import annotations

import json

import pytest

from chessarena.models import (
    COMPLETED,
    EngineBuild,
    EngineChannel,
    EnginePreset,
    EngineVersion,
    Game,
    Tournament,
    utcnow,
)
from chessarena.services import ratings, versions
from chessarena.services.versions import VersionError


def _manifest(registered) -> dict:
    return json.loads(
        (registered["build_dir"] / "manifest.json").read_text(encoding="utf-8")
    )


def _create_rated_with_sides(engine_factory, tournament_factory, side_a, side_b,
                             wins=4, losses=0, draws=0, tc="blitz_3_2"):
    """COMPLETED rated tournament with frozen snapshot sides + real games."""
    pairs = (wins + draws + losses) // 2
    tid = tournament_factory(
        name="rated", pairs=max(pairs, 1), time_control=tc, status=COMPLETED
    )
    with engine_factory() as session:
        t = session.query(Tournament).filter(Tournament.id == tid).one()
        t.completed_pairs = max(pairs, 1)
        snap = dict(t.config_snapshot or {})
        snap["engine_a"] = dict(side_a)
        snap["engine_b"] = dict(side_b)
        t.candidate_wins = wins
        t.candidate_losses = losses
        t.draws = draws
        t.finished_at = utcnow()
        t.arena_elo_enabled = True
        t.config_snapshot = snap
        pair = t.pair_jobs[0]
        pair.status = "COMPLETED"
        pair.return_code = 0
        seq = [1.0] * wins + [0.5] * draws + [0.0] * losses
        for i, score in enumerate(seq):
            game_number = i + 1
            a_white = game_number % 2 == 1
            result = ("1-0" if score == 1.0 else "1/2-1/2" if score == 0.5
                      else "0-1") if a_white else (
                "0-1" if score == 1.0 else "1/2-1/2" if score == 0.5 else "1-0")
            session.add(Game(
                tournament_id=tid, pair_job_id=pair.id,
                game_number=game_number,
                white_engine="EngineA" if a_white else "EngineB",
                black_engine="EngineB" if a_white else "EngineA",
                opening_index=0, result=result,
                pgn_path="/unused/rated.pgn", verified=True,
            ))
        session.commit()
    return tid


def _preset_side(session) -> dict:
    preset = session.query(EnginePreset).filter(
        EnginePreset.preset_id == "chessengine-production"
    ).one()
    build = session.query(EngineBuild).filter(
        EngineBuild.build_id == preset.build_id
    ).one()
    return {
        "preset_id": preset.preset_id,
        "display_name": preset.display_name,
        "build_id": build.build_id,
        "command_args": list(preset.command_args or []),
        "uci_options": dict(preset.uci_options or {}),
        "binary_sha256": build.binary_sha256,
    }


# ---------------------------------------------------------------------------
# Creation / immutability
# ---------------------------------------------------------------------------
def test_create_version_from_build_and_preset(engine_factory, registered):
    manifest = _manifest(registered)
    with engine_factory() as session:
        v = versions.create_version_from_build(
            session, version_id="ce-prod", display_name="Prod",
            build_id=manifest["build_id"], command_args=[], status="production",
        )
        assert v.binary_sha256 == manifest["binary_sha256"]
        assert v.source_sha == manifest["git_sha"]
        assert v.command_args == []
        v2 = versions.create_version_from_preset(
            session, version_id="ce-hist", display_name="Hist",
            preset_id="chessengine-production", status="historical",
        )
        assert list(v2.command_args) == ["--profile", "current-final"]
        assert v.identity_fingerprint != v2.identity_fingerprint


def test_duplicate_immutable_fingerprint_rejected(engine_factory, registered):
    with engine_factory() as session:
        versions.create_version_from_preset(
            session, version_id="ce-a", display_name="A",
            preset_id="chessengine-production")
        with pytest.raises(VersionError):
            versions.create_version_from_preset(
                session, version_id="ce-b", display_name="B",
                preset_id="chessengine-production")


def test_unknown_build_rejected(engine_factory):
    with engine_factory() as session:
        with pytest.raises(VersionError):
            versions.create_version_from_build(
                session, version_id="x", display_name="x", build_id="nope")


def test_invalid_status_rejected(engine_factory, registered):
    manifest = _manifest(registered)
    with engine_factory() as session:
        with pytest.raises(VersionError):
            versions.create_version_from_build(
                session, version_id="x", display_name="x",
                build_id=manifest["build_id"], status="bad-status")


def test_preset_mutation_does_not_affect_version(engine_factory, registered):
    with engine_factory() as session:
        v = versions.create_version_from_preset(
            session, version_id="ce-imm", display_name="Imm",
            preset_id="chessengine-production")
        fp, args = v.identity_fingerprint, list(v.command_args)
        preset = session.query(EnginePreset).filter(
            EnginePreset.preset_id == "chessengine-production").one()
        preset.command_args = ["--profile", "something-else"]
        session.commit()
        fresh = session.query(EngineVersion).filter(
            EngineVersion.version_id == "ce-imm").one()
        assert fresh.identity_fingerprint == fp
        assert list(fresh.command_args) == args


# ---------------------------------------------------------------------------
# Rating identity resolution
# ---------------------------------------------------------------------------
def test_legacy_snapshot_maps_to_version(engine_factory, registered,
                                         tournament_factory):
    with engine_factory() as session:
        versions.create_version_from_preset(
            session, version_id="ce-currentfinal-20260806",
            display_name="CurrentFinal",
            preset_id="chessengine-production", status="historical",
            # A KNOWN past production is registered directly as a public
            # rated historical participant (the explicit override the V2.1
            # controlled lifecycle allows).
            public_visible=True, rating_enabled=True)
        side = _preset_side(session)
    _create_rated_with_sides(
        engine_factory, tournament_factory, side_a=side, side_b={
            "preset_id": "stockfish-limited-2000",
            "display_name": "Stockfish Limited 2000",
            "build_id": "stockfish-build",
            "uci_options": {"UCI_LimitStrength": True, "UCI_Elo": 2000},
        })
    with engine_factory() as session:
        rows = ratings.compute_ratings(session)["blitz_3_2"]["engines"]
        row = next(r for r in rows if r["display_name"] == "CurrentFinal")
        assert row["participant_id"] == "ce-currentfinal-20260806"
        assert row["games"] == 4


def test_unknown_legacy_config_legacy_prefix(engine_factory, registered,
                                             tournament_factory):
    custom = {
        "preset_id": "archived-engine",
        "display_name": "Archived Engine",
        "build_id": "engine-build",
        "command_args": ["--profile", "old-profile"],
        "uci_options": {},
        "binary_sha256": "archived-sha",
    }
    _create_rated_with_sides(engine_factory, tournament_factory,
                             side_a=custom, side_b={
            "preset_id": "stockfish-limited-2000",
            "display_name": "Stockfish Limited 2000",
            "build_id": "stockfish-build",
            "uci_options": {"UCI_LimitStrength": True, "UCI_Elo": 2000},
        })
    with engine_factory() as session:
        rows = ratings.compute_ratings(session)["blitz_3_2"]["engines"]
        row = next(r for r in rows if r["display_name"] == "Archived Engine")
        assert row["participant_id"].startswith("legacy:")
        assert row["games"] == 4


def test_rated_version_vs_version(engine_factory, registered,
                                  tournament_factory):
    manifest = _manifest(registered)
    with engine_factory() as session:
        va = versions.create_version_from_build(
            session, version_id="ce-va", display_name="Engine A",
            build_id=manifest["build_id"], command_args=[], status="production")
        vb = versions.create_version_from_preset(
            session, version_id="ce-vb", display_name="Engine B",
            preset_id="chessengine-production", status="historical")
        side_a = versions.version_to_side(va)
        side_b = versions.version_to_side(vb)
    _create_rated_with_sides(engine_factory, tournament_factory,
                             side_a=side_a, side_b=side_b)
    with engine_factory() as session:
        rows = ratings.compute_ratings(session)["blitz_3_2"]["engines"]
        by_id = {r["participant_id"]: r for r in rows}
        assert by_id["ce-va"]["games"] == 4
        assert by_id["ce-vb"]["games"] == 4
        assert by_id["ce-va"]["rating"] > 1800  # 4W vs 1800 baseline
        assert by_id["ce-vb"]["rating"] < 1800


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------
def test_channel_set_and_target_validation(engine_factory, registered):
    manifest = _manifest(registered)
    with engine_factory() as session:
        versions.create_version_from_build(
            session, version_id="ce-prod", display_name="Prod",
            build_id=manifest["build_id"], status="production")
        ch = versions.set_channel(session, "current-final", "ce-prod")
        assert ch.channel_id == "current-final"
        assert ch.engine_version_id == "ce-prod"
        with pytest.raises(VersionError):
            versions.set_channel(session, "current-final", "does-not-exist")
        got = versions.get_channel(session, "current-final")
        assert got.engine_version_id == "ce-prod"


# ---------------------------------------------------------------------------
# API (V2.1 controlled lifecycle)
# ---------------------------------------------------------------------------
def test_api_create_list_get_version(engine_factory, registered, app_client):
    manifest = _manifest(registered)
    r = app_client.post("/chessarena/api/v1/engine-versions", json={
        "version_id": "ce-api-cand",
        "display_name": "API Cand",
        "build_id": manifest["build_id"],
        "command_args": [],
    })
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    assert body["version_id"] == "ce-api-cand"
    assert body["identity_fingerprint"]
    # Controlled lifecycle: HTTP-created versions are candidate/hidden/unrated.
    assert body["status"] == "candidate"
    assert body["public_visible"] is False
    assert body["rating_enabled"] is False
    got = app_client.get("/chessarena/api/v1/engine-versions/ce-api-cand")
    assert got.status_code == 200
    assert got.json()["binary_sha256"] == manifest["binary_sha256"]
    listing = app_client.get("/chessarena/api/v1/engine-versions").json()
    assert any(v["version_id"] == "ce-api-cand" for v in listing)
    missing = app_client.get("/chessarena/api/v1/engine-versions/nope")
    assert missing.status_code == 404


def test_api_cannot_create_production_directly(engine_factory, registered,
                                               app_client):
    """P1-1a: the HTTP surface cannot mint production/historical/public/
    rated participants — the schema rejects the statuses and the endpoint
    forces hidden/unrated regardless of any smuggled flags."""
    manifest = _manifest(registered)
    for bad_status in ("production", "historical"):
        r = app_client.post("/chessarena/api/v1/engine-versions", json={
            "version_id": f"ce-api-bad-{bad_status}",
            "display_name": "Bad",
            "build_id": manifest["build_id"],
            "status": bad_status,
        })
        assert r.status_code == 422, (bad_status, r.text[:200])
    # smuggled lifecycle flags are accepted by the model for compat but the
    # endpoint forces the controlled defaults: candidate/hidden/unrated.
    r = app_client.post("/chessarena/api/v1/engine-versions", json={
        "version_id": "ce-api-smuggled",
        "display_name": "Smuggled",
        "build_id": manifest["build_id"],
        "status": "candidate",
        "public_visible": True,
        "rating_enabled": True,
    })
    assert r.status_code == 200, r.text[:300]
    assert r.json()["status"] == "candidate"
    assert r.json()["public_visible"] is False
    assert r.json()["rating_enabled"] is False


def test_api_create_version_from_preset_and_duplicate(engine_factory,
                                                      registered, app_client):
    r = app_client.post("/chessarena/api/v1/engine-versions", json={
        "version_id": "ce-api-hist",
        "display_name": "API Hist",
        "preset_id": "chessengine-production",
    })
    assert r.status_code == 200, r.text[:300]
    assert r.json()["status"] == "candidate"  # controlled default
    dup = app_client.post("/chessarena/api/v1/engine-versions", json={
        "version_id": "ce-api-hist2",
        "display_name": "API Hist2",
        "preset_id": "chessengine-production",
    })
    assert dup.status_code == 422  # duplicate immutable fingerprint


def test_api_channel_put_rejected_promote_is_controlled(
    engine_factory, registered, app_client
):
    """P1-1b: generic channel repoint is removed (405); the controlled
    promote endpoint runs the full atomic lifecycle transition."""
    manifest = _manifest(registered)
    # old production on the channel (registered via the service with the
    # known-past-production explicit flags)
    with engine_factory() as session:
        versions.create_version_from_build(
            session, version_id="ce-ch-old", display_name="Ch Old",
            build_id=manifest["build_id"], command_args=[],
            status="production", rating_enabled=True, public_visible=True,
        )
        versions.set_channel(session, "current-final", "ce-ch-old")
        # target candidate: default launch identity on a second build
        _default_identity_target(engine_factory, registered,
                                 version_id="ce-ch-cand",
                                 display_name="Ch Cand")

    # generic repoint: gone, 405, and it mutated nothing
    r = app_client.put("/chessarena/api/v1/engine-channels/current-final",
                       json={"engine_version_id": "ce-ch-cand"})
    assert r.status_code == 405, r.text[:200]
    with engine_factory() as session:
        assert versions.get_channel(
            session, "current-final").engine_version_id == "ce-ch-old"

    # controlled promotion endpoint: full lifecycle transition
    r = app_client.post(
        "/chessarena/api/v1/engine-channels/current-final/promote",
        json={"engine_version_id": "ce-ch-cand"})
    assert r.status_code == 200, r.text[:300]
    assert r.json()["engine_version_id"] == "ce-ch-cand"
    with engine_factory() as session:
        assert versions.get_version(
            session, "ce-ch-old").status == "historical"
        tgt = versions.get_version(session, "ce-ch-cand")
        assert tgt.status == "production"
        assert tgt.public_visible is True
        assert tgt.rating_enabled is True
        assert versions.get_channel(
            session, "current-final").engine_version_id == "ce-ch-cand"

    # promoting an unknown target: 422 fail-closed
    r = app_client.post(
        "/chessarena/api/v1/engine-channels/current-final/promote",
        json={"engine_version_id": "nope"})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Version tournament
# ---------------------------------------------------------------------------
def test_version_tournament_freezes_identity(engine_factory, registered,
                                             app_client):
    manifest = _manifest(registered)
    opening = json.loads(
        (registered["opening_dir"] / "manifest.json").read_text(encoding="utf-8")
    )
    # Register a production participant for tournament selection via the
    # service (known-past-production explicit flags; the HTTP surface can
    # no longer mint production).
    with engine_factory() as session:
        versions.create_version_from_build(
            session, version_id="ce-tour-prod", display_name="Tour Prod",
            build_id=manifest["build_id"], command_args=[],
            status="production", rating_enabled=True, public_visible=True,
        )
    payload = {
        "name": "version-vs-version",
        "engine_a": {"version_id": "ce-tour-prod"},
        "engine_b": {"version_id": "ce-tour-prod"},
        "opening_set_id": opening["opening_set_id"],
        "time_control": "blitz_3_2",
        "pairs": 2,
        "allow_intentional_self_play": True,
    }
    r = app_client.post("/chessarena/api/v1/tournaments", json=payload)
    assert r.status_code == 201, r.text[:400]
    snap = r.json()["config_snapshot"]
    for side in (snap["engine_a"], snap["engine_b"]):
        assert side["version_id"] == "ce-tour-prod"
        assert side["identity_fingerprint"]
        assert side["command_args"] == []
        assert side["binary_sha256"] == manifest["binary_sha256"]
        assert side["source_sha"] == manifest["git_sha"]


# ---------------------------------------------------------------------------
# V2.1: generic EngineVersion lifecycle + atomic channel promotion
# ---------------------------------------------------------------------------
def _immutable_fields(v):
    return {
        "version_id": v.version_id,
        "display_name": v.display_name,
        "build_id": v.build_id,
        "command_args": list(v.command_args or []),
        "uci_options": dict(v.uci_options or {}),
        "source_sha": v.source_sha,
        "binary_sha256": v.binary_sha256,
        "identity_fingerprint": v.identity_fingerprint,
    }


def _setup_promotion_scene(engine_factory, registered):
    """Old production on the channel + a fresh candidate build version."""
    manifest = _manifest(registered)
    with engine_factory() as session:
        old = versions.create_version_from_build(
            session, version_id="ce-old-prod", display_name="Old Prod",
            build_id=manifest["build_id"], command_args=[],
            status="production", rating_enabled=True, public_visible=True,
        )
        versions.set_channel(session, "current-final", old.version_id)
    return manifest, "ce-old-prod"


def _default_identity_target(session_factory, registered,
                              version_id="ce-cand",
                              display_name="Cand"):
    """Create a PROMOTABLE candidate: default launch identity
    (command_args=[], uci_options={}) on a SECOND registered build so its
    fingerprint differs from the scene's old production.  Returns the
    version_id; commits happen inside."""
    import hashlib
    import json as _json
    from pathlib import Path
    from chessarena.models import EngineBuild

    build_dir = Path(registered["build_dir"]).parent / "build2"
    build_dir.mkdir(parents=True, exist_ok=True)
    content = b"second dummy engine binary for promotion tests"
    (build_dir / "engine").write_bytes(content)
    m2 = {
        "schema_version": 1,
        "build_id": "build2-x86_64",
        "git_sha": "b" * 40,
        "binary_sha256": hashlib.sha256(content).hexdigest(),
    }
    (build_dir / "manifest.json").write_text(_json.dumps(m2))
    with session_factory() as session:
        existing = (
            session.query(EngineBuild)
            .filter(EngineBuild.build_id == "build2-x86_64")
            .first()
        )
        if existing is None:
            session.add(EngineBuild(
                build_id="build2-x86_64", engine_name="Test",
                git_sha=m2["git_sha"],
                binary_path=str(build_dir / "engine"),
                binary_sha256=m2["binary_sha256"], platform="x86_64",
                supported_profiles=[], manifest=m2, enabled=True,
            ))
            session.commit()
        target = versions.create_version_from_build(
            session, version_id=version_id, display_name=display_name,
            build_id="build2-x86_64", command_args=[], uci_options={},
            status="candidate",
        )
        return target.version_id


def test_v21_create_from_build_defaults_candidate_hidden_unrated(
    engine_factory, registered
):
    """(1) create from build -> candidate / hidden / unrated, args=[] and
    opts={}, source+binary identity copied from the registered build."""
    manifest = _manifest(registered)
    with engine_factory() as session:
        v = versions.create_version_from_build(
            session, version_id="ce-new-cand", display_name="New Cand",
            build_id=manifest["build_id"],
        )
        assert v.status == "candidate"
        assert v.public_visible is False
        assert v.rating_enabled is False
        assert list(v.command_args) == []
        assert dict(v.uci_options) == {}
        assert v.source_sha == manifest["git_sha"]
        assert v.binary_sha256 == manifest["binary_sha256"]


def test_v21_duplicate_version_id_rejected(engine_factory, registered):
    """(2) duplicate version_id fails closed."""
    manifest = _manifest(registered)
    with engine_factory() as session:
        versions.create_version_from_build(
            session, version_id="ce-dup", display_name="Dup",
            build_id=manifest["build_id"],
        )
        with pytest.raises(Exception):
            versions.create_version_from_build(
                session, version_id="ce-dup", display_name="Dup 2",
                build_id=manifest["build_id"],
            )


def test_v21_duplicate_fingerprint_rejected_on_cli_defaults(
    engine_factory, registered
):
    """(3) duplicate immutable fingerprint (same build, same empty launch
    config) fails closed."""
    manifest = _manifest(registered)
    with engine_factory() as session:
        versions.create_version_from_build(
            session, version_id="ce-fp-a", display_name="FP A",
            build_id=manifest["build_id"],
        )
        with pytest.raises(VersionError):
            versions.create_version_from_build(
                session, version_id="ce-fp-b", display_name="FP B",
                build_id=manifest["build_id"],
            )


def test_v21_promotion_dry_run_zero_mutation(engine_factory, registered):
    """(4) plan_channel_promotion performs ZERO DB mutation."""
    manifest, old_id = _setup_promotion_scene(engine_factory, registered)
    # PROMOTABLE target: default launch identity on a second build.
    target_id = _default_identity_target(engine_factory, registered)
    with engine_factory() as session:
        target = versions.get_version(session, target_id)
        before = {
            "old": _immutable_fields(
                versions.get_version(session, old_id)),
            "target": _immutable_fields(target),
            "channel": versions.get_channel(
                session, "current-final").engine_version_id,
        }

    with engine_factory() as session:
        plan = versions.plan_channel_promotion(
            session, "current-final", target_id)
        assert plan.ok, plan["errors"]
        assert plan["current"]["version_id"] == old_id
        assert plan["target"]["version_id"] == target_id
        assert plan["after"]["channel_points_to"] == target_id
        assert plan["after"]["old_status"] == "historical"
        # zero mutation: statuses, channel pointer, lifecycle flags intact
        old = versions.get_version(session, old_id)
        tgt = versions.get_version(session, target_id)
        assert old.status == "production"
        assert tgt.status == "candidate"
        assert tgt.public_visible is False
        assert tgt.rating_enabled is False
        assert versions.get_channel(
            session, "current-final").engine_version_id == old_id
        assert _immutable_fields(old) == before["old"]
        assert _immutable_fields(tgt) == before["target"]


def test_v21_promotion_commit_full_transition(engine_factory, registered):
    """(5) promote_channel: old production -> historical, target candidate ->
    production + public + rated, channel -> target."""
    manifest, old_id = _setup_promotion_scene(engine_factory, registered)
    # PROMOTABLE target: default launch identity on a second build
    target_id = _default_identity_target(engine_factory, registered)
    with engine_factory() as session:
        target = versions.get_version(session, target_id)
        target_id = target.version_id
        versions.promote_channel(session, "current-final", target_id)

        old = versions.get_version(session, old_id)
        tgt = versions.get_version(session, target_id)
        assert old.status == "historical"
        # old production keeps its public/rated flags (history participant)
        assert old.public_visible is True
        assert old.rating_enabled is True
        assert tgt.status == "production"
        assert tgt.public_visible is True
        assert tgt.rating_enabled is True
        assert versions.get_channel(
            session, "current-final").engine_version_id == target_id


def test_v21_promotion_preserves_immutable_fields(engine_factory, registered):
    """(6) immutable fields byte-for-byte identical before/after promotion."""
    manifest, old_id = _setup_promotion_scene(engine_factory, registered)
    # PROMOTABLE target: default launch identity on a second build
    target_id = _default_identity_target(engine_factory, registered)
    with engine_factory() as session:
        target = versions.get_version(session, target_id)
        target_id = target.version_id
        old_before = _immutable_fields(versions.get_version(session, old_id))
        tgt_before = _immutable_fields(target)

        versions.promote_channel(session, "current-final", target_id)

        assert _immutable_fields(
            versions.get_version(session, old_id)) == old_before
        assert _immutable_fields(
            versions.get_version(session, target_id)) == tgt_before


def test_v21_failed_promotion_no_partial_state(engine_factory, registered):
    """(7) failed promotion (invalid target) leaves NO partial demotion and
    NO channel drift."""
    manifest, old_id = _setup_promotion_scene(engine_factory, registered)
    with engine_factory() as session:
        with pytest.raises(VersionError):
            versions.promote_channel(
                session, "current-final", "does-not-exist")
        old = versions.get_version(session, old_id)
        assert old.status == "production"  # not demoted
        assert versions.get_channel(
            session, "current-final").engine_version_id == old_id
        # also: target already historical is rejected
        hist = versions.create_version_from_preset(
            session, version_id="ce-hist-x", display_name="Hist X",
            preset_id="chessengine-production", status="historical",
        )
        with pytest.raises(VersionError):
            versions.promote_channel("current-final", "ce-hist-x") if False \
                else versions.promote_channel(
                    session, "current-final", hist.version_id)
        assert versions.get_channel(
            session, "current-final").engine_version_id == old_id


def test_v21_promotion_rejects_noop_and_unknown_channel(
    engine_factory, registered
):
    """(8) unknown channel / unknown target / already-pointing fail closed:
    the planner reports errors without raising; promote_channel raises."""
    manifest, old_id = _setup_promotion_scene(engine_factory, registered)
    with engine_factory() as session:
        plan = versions.plan_channel_promotion(
            session, "no-such-channel", "ce-old-prod")
        assert not plan.ok
        plan = versions.plan_channel_promotion(
            session, "current-final", old_id)
        assert not plan.ok  # already points at it
        plan = versions.plan_channel_promotion(
            session, "current-final", "does-not-exist")
        assert not plan.ok
        # promote_channel turns the same errors into exceptions.
        with pytest.raises(VersionError):
            versions.promote_channel(session, "no-such-channel", old_id)
        with pytest.raises(VersionError):
            versions.promote_channel(session, "current-final", old_id)
        with pytest.raises(VersionError):
            versions.promote_channel(
                session, "current-final", "does-not-exist")


def test_v21_create_from_preset_snapshot_no_drift(engine_factory, registered):
    """(9) creating from preset snapshots the launch config; later preset
    edits never drift the version (and CLI --from-preset defaults to the
    controlled candidate lifecycle)."""
    with engine_factory() as session:
        v = versions.create_version_from_preset(
            session, version_id="ce-psnap", display_name="P Snap",
            preset_id="chessengine-production",
            status="candidate", rating_enabled=False, public_visible=False,
        )
        assert list(v.command_args) == ["--profile", "current-final"]
        assert v.status == "candidate"
        assert v.public_visible is False
        assert v.rating_enabled is False
        frozen = _immutable_fields(v)
        preset = session.query(EnginePreset).filter(
            EnginePreset.preset_id == "chessengine-production").one()
        preset.command_args = ["--profile", "totally-different"]
        preset.uci_options = {"Hash": 999}
        session.commit()
        fresh = versions.get_version(session, "ce-psnap")
        assert _immutable_fields(fresh) == frozen


def test_v21_promotion_does_not_touch_frozen_snapshots(
    engine_factory, registered, tournament_factory
):
    """(10) channel promotion never alters existing tournament frozen
    snapshots or ACTIVE HumanGame opponent snapshots."""
    from chessarena.models import HumanGame, HumanGameMove

    manifest, old_id = _setup_promotion_scene(engine_factory, registered)
    # a tournament frozen against the OLD production identity
    side = {
        "version_id": old_id,
        "display_name": "Old Prod",
        "build_id": manifest["build_id"],
        "command_args": [],
        "uci_options": {},
        "binary_sha256": manifest["binary_sha256"],
        "source_sha": manifest["git_sha"],
    }
    tid = tournament_factory(name="frozen", pairs=1, status="QUEUED")
    with engine_factory() as session:
        t = session.query(Tournament).filter(Tournament.id == tid).one()
        snap = dict(t.config_snapshot or {})
        snap["engine_a"] = dict(side)
        t.config_snapshot = snap
        # an ACTIVE human game frozen from the channel
        session.add(HumanGame(
            id="hg-frozen", game_token_hash="x" * 64,
            opponent_kind="engine", opponent_ref="channel:current-final",
            opponent_snapshot={"version_id": old_id, "kind": "engine",
                               "display_name": "Old Prod"},
            human_color="white", status="ACTIVE",
            current_fen="start", revision=0, engine_pending=False,
            creator_ip="198.51.100.9",
            created_at=utcnow(), last_move_at=utcnow(),
            expires_at=utcnow(), idle_expires_at=utcnow(),
        ))
        session.commit()
        frozen_tournament_snapshot = json.dumps(
            t.config_snapshot, sort_keys=True)

    target_id = _default_identity_target(engine_factory, registered)
    with engine_factory() as session:
        versions.promote_channel(session, "current-final", target_id)

    with engine_factory() as session:
        t = session.query(Tournament).filter(Tournament.id == tid).one()
        assert json.dumps(t.config_snapshot, sort_keys=True) == \
            frozen_tournament_snapshot
        hg = session.get(HumanGame, "hg-frozen")
        assert hg.opponent_snapshot["version_id"] == old_id
        # the NEXT game through the channel resolves to the new production
        assert versions.get_channel(
            session, "current-final").engine_version_id == target_id


# ---------------------------------------------------------------------------
# V2.1: admin CLI
# ---------------------------------------------------------------------------
def test_v21_admin_cli_engine_version_create_and_promote(
    engine_factory, registered, settings
):
    """CLI end-to-end: argparse subcommands, candidate defaults, dry-run
    zero-mutation, --yes atomic commit, fail-closed exits."""
    from chessarena import admin

    # Point the CLI at the SAME tmp sqlite db the fixtures use.
    db_url = settings.db_url
    cli_settings = type(settings)(**{
        **{f.name: getattr(settings, f.name)
           for f in settings.__dataclass_fields__.values()},
        "db_url": db_url,
    })

    manifest = _manifest(registered)
    # a SECOND registered build so a default-identity (args=[], opts={})
    # version on it has a distinct fingerprint from the old production.
    import hashlib
    import json as _json
    from pathlib import Path
    from chessarena.models import EngineBuild

    build_dir = Path(registered["build_dir"]).parent / "build2"
    build_dir.mkdir(parents=True, exist_ok=True)
    content = b"second dummy engine binary for cli lifecycle test"
    (build_dir / "engine").write_bytes(content)
    m2 = {
        "schema_version": 1,
        "build_id": "build2-x86_64",
        "git_sha": "b" * 40,
        "binary_sha256": hashlib.sha256(content).hexdigest(),
    }
    (build_dir / "manifest.json").write_text(_json.dumps(m2))

    # old production on the channel
    with engine_factory() as session:
        session.add(EngineBuild(
            build_id="build2-x86_64", engine_name="Test", git_sha=m2["git_sha"],
            binary_path=str(build_dir / "engine"),
            binary_sha256=m2["binary_sha256"], platform="x86_64",
            supported_profiles=[], manifest=m2, enabled=True,
        ))
        session.commit()
        versions.create_version_from_build(
            session, version_id="ce-old-prod", display_name="Old Prod",
            build_id=manifest["build_id"], command_args=[],
            status="production", rating_enabled=True, public_visible=True,
        )
        versions.set_channel(session, "current-final", "ce-old-prod")

    # create --from-preset: candidate / hidden / unrated, frozen args
    # (preset snapshots stay experiment-grade identities — they can never
    # pass the production launch-identity gate below)
    rc = admin.main([
        "engine-version", "create",
        "--from-preset", "chessengine-production",
        "--version", "ce-cli-cand",
        "--name", "CLI Candidate",
    ], settings=cli_settings)
    assert rc == 0
    with engine_factory() as session:
        v = versions.get_version(session, "ce-cli-cand")
        assert v is not None
        assert v.status == "candidate"
        assert v.public_visible is False
        assert v.rating_enabled is False
        assert list(v.command_args) == ["--profile", "current-final"]

    # duplicate fingerprint (same preset again) fails closed with exit 2
    rc = admin.main([
        "engine-version", "create",
        "--from-preset", "chessengine-production",
        "--version", "ce-cli-dup",
        "--name", "CLI Dup",
    ], settings=cli_settings)
    assert rc == 2

    # a preset-derived (profile) candidate can NEVER be promoted
    rc = admin.main([
        "engine-channel", "promote", "current-final", "ce-cli-cand",
    ], settings=cli_settings)
    assert rc == 2  # production launch-identity gate: profile args

    # create the DEFAULT-identity promotion target from the second build
    rc = admin.main([
        "engine-version", "create",
        "--build", "build2-x86_64",
        "--version", "ce-cli-default",
        "--name", "CLI Default Identity",
    ], settings=cli_settings)
    assert rc == 0
    with engine_factory() as session:
        v = versions.get_version(session, "ce-cli-default")
        assert list(v.command_args) == []
        assert dict(v.uci_options) == {}

    # dry-run: zero mutation
    rc = admin.main([
        "engine-channel", "promote", "current-final", "ce-cli-default",
    ], settings=cli_settings)
    assert rc == 0
    with engine_factory() as session:
        assert versions.get_channel(
            session, "current-final").engine_version_id == "ce-old-prod"
        assert versions.get_version(
            session, "ce-cli-default").status == "candidate"

    # --yes: atomic promotion
    rc = admin.main([
        "engine-channel", "promote", "current-final", "ce-cli-default",
        "--yes",
    ], settings=cli_settings)
    assert rc == 0
    with engine_factory() as session:
        assert versions.get_channel(
            session, "current-final").engine_version_id == "ce-cli-default"
        assert versions.get_version(
            session, "ce-old-prod").status == "historical"
        assert versions.get_version(
            session, "ce-cli-default").status == "production"
        assert versions.get_version(
            session, "ce-cli-default").public_visible is True
        assert versions.get_version(
            session, "ce-cli-default").rating_enabled is True

    # promote to an unknown version: fail closed, exit code 2, no mutation
    rc = admin.main([
        "engine-channel", "promote", "current-final", "nope", "--yes",
    ], settings=cli_settings)
    assert rc == 2
    with engine_factory() as session:
        assert versions.get_channel(
            session, "current-final").engine_version_id == "ce-cli-default"

    # --build create with a fresh build id fails closed when unknown
    rc = admin.main([
        "engine-version", "create",
        "--build", "no-such-build",
        "--version", "ce-cli-x",
        "--name", "X",
    ], settings=cli_settings)
    assert rc == 2


# ---------------------------------------------------------------------------
# V2.1 Repair 1: production gate + controlled surface regressions
# ---------------------------------------------------------------------------
def test_v21r_disabled_target_build_blocks_promotion(
    engine_factory, registered
):
    """P1-2: a build disabled after the candidate was created blocks BOTH
    the dry-run (plan.ok false) and the real promotion; nothing changes."""
    manifest, old_id = _setup_promotion_scene(engine_factory, registered)
    # PROMOTABLE target: default launch identity on a second build
    target_id = _default_identity_target(engine_factory, registered)
    with engine_factory() as session:
        target = versions.get_version(session, target_id)
        # disable the target's build AFTER creation
        build = session.query(EngineBuild).filter(
            EngineBuild.build_id == target.build_id).one()
        build.enabled = False
        session.commit()

        plan = versions.plan_channel_promotion(
            session, "current-final", target.version_id)
        assert not plan.ok
        assert any("disabled" in e for e in plan["errors"])

        with pytest.raises(VersionError):
            versions.promote_channel(
                session, "current-final", target.version_id)

        # nothing changed: old still production, channel untouched,
        # candidate untouched
        assert versions.get_version(
            session, old_id).status == "production"
        assert versions.get_channel(
            session, "current-final").engine_version_id == old_id
        tgt = versions.get_version(session, target.version_id)
        assert tgt.status == "candidate"
        assert tgt.public_visible is False
        assert tgt.rating_enabled is False


def test_v21r_provenance_mismatch_blocks_promotion(
    engine_factory, registered
):
    """P1-2b: the promotion gate re-verifies the version's frozen
    provenance against the CURRENT registry; a drifted build row blocks
    promotion."""
    manifest, old_id = _setup_promotion_scene(engine_factory, registered)
    # PROMOTABLE target: default launch identity on a second build
    target_id = _default_identity_target(engine_factory, registered)
    with engine_factory() as session:
        target = versions.get_version(session, target_id)
        # simulate registry drift: the build's recorded binary changes
        build = session.query(EngineBuild).filter(
            EngineBuild.build_id == target.build_id).one()
        build.binary_sha256 = "0" * 64
        session.commit()

        plan = versions.plan_channel_promotion(
            session, "current-final", target.version_id)
        assert not plan.ok
        assert any("provenance mismatch" in e for e in plan["errors"])
        with pytest.raises(VersionError):
            versions.promote_channel(
                session, "current-final", target.version_id)
        assert versions.get_channel(
            session, "current-final").engine_version_id == old_id


def test_v21r_cli_exactly_one_source(engine_factory, registered, settings,
                                     capsys):
    """P2-1: --build and --from-preset are mutually exclusive at the
    parser level."""
    from chessarena import admin

    cli_settings = type(settings)(**{
        **{f.name: getattr(settings, f.name)
           for f in settings.__dataclass_fields__.values()},
        "db_url": settings.db_url,
    })
    with pytest.raises(SystemExit) as excinfo:
        admin.main([
            "engine-version", "create",
            "--build", "some-build",
            "--from-preset", "chessengine-production",
            "--version", "ce-both",
            "--name", "Both",
        ], settings=cli_settings)
    assert excinfo.value.code == 2  # argparse usage error
    out = capsys.readouterr()
    assert "not allowed with argument" in (out.err + out.out)


def test_v21r_impact_counts_are_target_specific(
    engine_factory, registered, tournament_factory
):
    """P2-2: rated-history and active-tournament counts only count
    tournaments whose frozen sides resolve to the target (via the shared
    participant resolver), not the whole database."""
    import hashlib
    import json as _json
    from pathlib import Path
    from chessarena.models import COMPLETED, EngineBuild

    manifest, old_id = _setup_promotion_scene(engine_factory, registered)
    # one rated COMPLETED tournament frozen against the OLD production
    old_side = {
        "version_id": old_id,
        "display_name": "Old Prod",
        "build_id": manifest["build_id"],
        "command_args": [],
        "uci_options": {},
        "binary_sha256": manifest["binary_sha256"],
        "source_sha": manifest["git_sha"],
    }
    tid = tournament_factory(name="old-rated", pairs=1, status=COMPLETED)
    # a default-identity target on a SECOND registered build (promotable)
    build_dir = Path(registered["build_dir"]).parent / "build2"
    build_dir.mkdir(parents=True, exist_ok=True)
    content = b"second dummy engine binary for impact counts test"
    (build_dir / "engine").write_bytes(content)
    m2 = {
        "schema_version": 1,
        "build_id": "build2-x86_64",
        "git_sha": "b" * 40,
        "binary_sha256": hashlib.sha256(content).hexdigest(),
    }
    (build_dir / "manifest.json").write_text(_json.dumps(m2))
    with engine_factory() as session:
        t = session.query(Tournament).filter(Tournament.id == tid).one()
        snap = dict(t.config_snapshot or {})
        snap["engine_a"] = dict(old_side)
        t.config_snapshot = snap
        t.arena_elo_enabled = True
        t.completed_pairs = 1
        t.finished_at = utcnow()
        session.add(EngineBuild(
            build_id="build2-x86_64", engine_name="Test", git_sha=m2["git_sha"],
            binary_path=str(build_dir / "engine"),
            binary_sha256=m2["binary_sha256"], platform="x86_64",
            supported_profiles=[], manifest=m2, enabled=True,
        ))
        session.commit()
        target = versions.create_version_from_build(
            session, version_id="ce-cand", display_name="Cand",
            build_id="build2-x86_64", command_args=[], uci_options={},
            status="candidate",
        )
        target_id = target.version_id

    # a legacy fingerprint-matching rated tournament for the TARGET:
    # same frozen config as the target (default identity, no version_id) —
    # the resolver must map it to the target via fingerprint.
    fp_side = {
        "preset_id": "cand-preset",
        "display_name": "Cand Preset",
        "build_id": "build2-x86_64",
        "command_args": [],
        "uci_options": {},
        "binary_sha256": m2["binary_sha256"],
        "source_sha": m2["git_sha"],
    }
    tid2 = tournament_factory(name="target-legacy-fp", pairs=1,
                              status=COMPLETED)
    # plus an unrelated rated COMPLETED tournament (stockfish anchors)
    tid3 = tournament_factory(name="unrelated-rated", pairs=1,
                              status=COMPLETED)
    with engine_factory() as session:
        t2 = session.query(Tournament).filter(Tournament.id == tid2).one()
        snap = dict(t2.config_snapshot or {})
        snap["engine_b"] = dict(fp_side)
        t2.config_snapshot = snap
        t2.arena_elo_enabled = True
        t2.completed_pairs = 1
        t2.finished_at = utcnow()
        t3 = session.query(Tournament).filter(Tournament.id == tid3).one()
        t3.arena_elo_enabled = True
        t3.completed_pairs = 1
        t3.finished_at = utcnow()
        session.commit()

        plan = versions.plan_channel_promotion(
            session, "current-final", target_id)
        assert plan.ok, plan["errors"]
        # ONLY the fingerprint-matching tournament counts for the target:
        # not the old production's history, not the unrelated tournament.
        assert plan["rated_history_matches_for_target"] == 1
        # No active tournaments reference either side in this scene.
        assert plan["active_tournaments_referencing_target"] == 0
        assert plan["active_tournaments_referencing_current"] == 0


# ---------------------------------------------------------------------------
# V2.1-A Repair 2: production launch-identity gate
# ---------------------------------------------------------------------------
def test_v21r2_profile_args_block_promotion(engine_factory, registered):
    """Repair 2: a candidate created with an explicit profile alias
    (however it was created — here via the service, the same shape HTTP
    can mint) can NEVER reach production: plan.ok false, promote raises,
    zero mutation on channel/old/target."""
    manifest, old_id = _setup_promotion_scene(engine_factory, registered)
    with engine_factory() as session:
        # same build as the old production, but with a profile alias —
        # a distinct fingerprint, so creation succeeds as candidate.
        target = versions.create_version_from_build(
            session, version_id="ce-fake-cf", display_name="Fake CurrentFinal",
            build_id=manifest["build_id"],
            command_args=["--profile", "current-final"],
            uci_options={}, status="candidate",
        )
        plan = versions.plan_channel_promotion(
            session, "current-final", target.version_id)
        assert not plan.ok
        assert any("command_args=[]" in e for e in plan["errors"])

        with pytest.raises(VersionError):
            versions.promote_channel(
                session, "current-final", target.version_id)

        # zero mutation
        assert versions.get_version(
            session, old_id).status == "production"
        assert versions.get_channel(
            session, "current-final").engine_version_id == old_id
        tgt = versions.get_version(session, "ce-fake-cf")
        assert tgt.status == "candidate"
        assert tgt.public_visible is False
        assert tgt.rating_enabled is False


def test_v21r2_nonempty_uci_options_block_promotion(
    engine_factory, registered
):
    """Repair 2: a candidate with non-default UCI options (e.g. Hash=999)
    also fails the production launch-identity gate."""
    manifest, old_id = _setup_promotion_scene(engine_factory, registered)
    with engine_factory() as session:
        target = versions.create_version_from_build(
            session, version_id="ce-hash999", display_name="Hash 999",
            build_id=manifest["build_id"],
            command_args=[], uci_options={"Hash": 999},
            status="candidate",
        )
        plan = versions.plan_channel_promotion(
            session, "current-final", target.version_id)
        assert not plan.ok
        assert any("uci_options={}" in e for e in plan["errors"])
        with pytest.raises(VersionError):
            versions.promote_channel(
                session, "current-final", target.version_id)
        assert versions.get_channel(
            session, "current-final").engine_version_id == old_id


def test_v21r2_default_identity_target_still_promotes(
    engine_factory, registered
):
    """Repair 2 does not over-block: the canonical onboarding shape —
    create from raw build with command_args=[] and uci_options={} —
    still plans and promotes cleanly (plan.ok true, full transition)."""
    manifest, old_id = _setup_promotion_scene(engine_factory, registered)
    # The scene's old production already uses the same build + [] + {},
    # so a NEW default-identity version on the same build would collide on
    # fingerprint. Build a second registered build to get a distinct
    # default-identity target.
    import hashlib
    import json as _json
    from pathlib import Path
    from chessarena.models import EngineBuild

    build_dir = Path(registered["build_dir"]).parent / "build2"
    build_dir.mkdir(parents=True, exist_ok=True)
    content = b"second dummy engine binary for identity-gate test"
    (build_dir / "engine").write_bytes(content)
    m2 = {
        "schema_version": 1,
        "build_id": "build2-x86_64",
        "git_sha": "b" * 40,
        "binary_sha256": hashlib.sha256(content).hexdigest(),
    }
    (build_dir / "manifest.json").write_text(_json.dumps(m2))
    with engine_factory() as session:
        session.add(EngineBuild(
            build_id="build2-x86_64", engine_name="Test", git_sha=m2["git_sha"],
            binary_path=str(build_dir / "engine"),
            binary_sha256=m2["binary_sha256"], platform="x86_64",
            supported_profiles=[], manifest=m2, enabled=True,
        ))
        session.commit()
        target = versions.create_version_from_build(
            session, version_id="ce-clean-default",
            display_name="Clean Default",
            build_id="build2-x86_64",
            command_args=[], uci_options={}, status="candidate",
        )
        plan = versions.plan_channel_promotion(
            session, "current-final", target.version_id)
        assert plan.ok, plan["errors"]
        versions.promote_channel(session, "current-final", target.version_id)
        assert versions.get_channel(
            session, "current-final").engine_version_id == \
            "ce-clean-default"
        assert versions.get_version(
            session, "ce-clean-default").status == "production"
