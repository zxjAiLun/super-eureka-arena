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
            preset_id="chessengine-production", status="historical")
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
# API
# ---------------------------------------------------------------------------
def test_api_create_list_get_version(engine_factory, registered, app_client):
    manifest = _manifest(registered)
    r = app_client.post("/chessarena/api/v1/engine-versions", json={
        "version_id": "ce-api-prod",
        "display_name": "API Prod",
        "build_id": manifest["build_id"],
        "command_args": [],
        "status": "production",
    })
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    assert body["version_id"] == "ce-api-prod"
    assert body["identity_fingerprint"]
    got = app_client.get("/chessarena/api/v1/engine-versions/ce-api-prod")
    assert got.status_code == 200
    assert got.json()["binary_sha256"] == manifest["binary_sha256"]
    listing = app_client.get("/chessarena/api/v1/engine-versions").json()
    assert any(v["version_id"] == "ce-api-prod" for v in listing)
    missing = app_client.get("/chessarena/api/v1/engine-versions/nope")
    assert missing.status_code == 404


def test_api_create_version_from_preset_and_duplicate(engine_factory,
                                                      registered, app_client):
    r = app_client.post("/chessarena/api/v1/engine-versions", json={
        "version_id": "ce-api-hist",
        "display_name": "API Hist",
        "preset_id": "chessengine-production",
        "status": "historical",
    })
    assert r.status_code == 200, r.text[:300]
    dup = app_client.post("/chessarena/api/v1/engine-versions", json={
        "version_id": "ce-api-hist2",
        "display_name": "API Hist2",
        "preset_id": "chessengine-production",
        "status": "historical",
    })
    assert dup.status_code == 422  # duplicate immutable fingerprint


def test_api_channel_put(engine_factory, registered, app_client):
    manifest = _manifest(registered)
    app_client.post("/chessarena/api/v1/engine-versions", json={
        "version_id": "ce-ch-prod", "display_name": "Ch Prod",
        "build_id": manifest["build_id"], "status": "production"})
    r = app_client.put("/chessarena/api/v1/engine-channels/current-final",
                       json={"engine_version_id": "ce-ch-prod"})
    assert r.status_code == 200, r.text[:300]
    assert r.json()["engine_version_id"] == "ce-ch-prod"
    bad = app_client.put("/chessarena/api/v1/engine-channels/current-final",
                         json={"engine_version_id": "nope"})
    assert bad.status_code == 422


# ---------------------------------------------------------------------------
# Version tournament
# ---------------------------------------------------------------------------
def test_version_tournament_freezes_identity(engine_factory, registered,
                                             app_client):
    manifest = _manifest(registered)
    opening = json.loads(
        (registered["opening_dir"] / "manifest.json").read_text(encoding="utf-8")
    )
    r = app_client.post("/chessarena/api/v1/engine-versions", json={
        "version_id": "ce-tour-prod", "display_name": "Tour Prod",
        "build_id": manifest["build_id"], "command_args": [],
        "status": "production"})
    assert r.status_code == 200, r.text[:300]
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
