"""S4.3E Phase 1 Repair 1: EngineVersion backfill fail-closed tests."""

from __future__ import annotations

import json
import subprocess

import pytest

from chessarena.models import EngineBuild, EngineChannel, EngineVersion
from chessarena.services import versions

from scripts import register_engine_versions as backfill


def _artifact_dir(tmp_path, build_id="20260811-26604c4-linux-x86_64",
                  git_sha="26604c425625d69e5b7e7b967db8926f4da01b8a",
                  binary_sha256="f0e8f91a3a0828a158672cecdf7859dbd9a3c9bac36b965bdcc90db31b51189d"):
    d = tmp_path / "artifact"
    d.mkdir()
    manifest = {
        "schema_version": 1,
        "build_id": build_id,
        "engine_name": "ChessEngineDemo",
        "git_sha": git_sha,
        "binary_sha256": binary_sha256,
        "platform": "linux-x86_64",
        "supported_profiles": ["current-final", "current"],
    }
    (d / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (d / "engine").write_bytes(b"dummy-binary")
    return d


def _register_build(engine_factory, manifest: dict):
    with engine_factory() as session:
        session.add(EngineBuild(
            build_id=manifest["build_id"],
            engine_name="ChessEngineDemo",
            git_sha=manifest["git_sha"],
            binary_path="/unused/engine",
            binary_sha256=manifest["binary_sha256"],
            platform="linux-x86_64",
            supported_profiles=["current-final", "current"],
            manifest=manifest,
            enabled=True,
        ))
        session.commit()


def test_backfill_missing_build_with_build_dir_works(engine_factory, tmp_path,
                                                     registered, monkeypatch):
    """The missing-build + --production-build-dir path must not NameError
    (regression: json was not imported) and must register + create."""
    artifact = _artifact_dir(tmp_path)

    def fake_install(argv, **kwargs):
        manifest = json.loads((artifact / "manifest.json").read_text())
        _register_build(engine_factory, manifest)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(backfill.subprocess, "run", fake_install)
    with engine_factory() as session:
        report = backfill.run_backfill(session, production_build_dir=artifact)
    assert "registered" in report["production_build"]
    assert "created ce-currentfinal-20260811" in report["production_version"]
    assert report["channel"] == "current-final -> ce-currentfinal-20260811"
    with engine_factory() as session:
        v = versions.get_version(session, "ce-currentfinal-20260811")
        assert v is not None
        assert v.identity_fingerprint == backfill.PRODUCTION["identity_fingerprint"]


def test_backfill_exact_build_idempotent(engine_factory, tmp_path, registered):
    """Existing exact build/version => idempotent success, channel pointed."""
    artifact = _artifact_dir(tmp_path)
    manifest = json.loads((artifact / "manifest.json").read_text())
    _register_build(engine_factory, manifest)
    with engine_factory() as session:
        first = backfill.run_backfill(session)
    assert "verified exact" in first["production_build"]
    assert "created ce-currentfinal-20260811" in first["production_version"]
    assert "created ce-currentfinal-20260806" in first["historical_version"]
    with engine_factory() as session:
        second = backfill.run_backfill(session)
    assert "verified exact" in second["production_build"]
    assert "already exists and verified exact" in second["production_version"]
    assert "already exists and verified exact" in second["historical_version"]
    assert second["channel"] == "current-final -> ce-currentfinal-20260811"


def test_backfill_wrong_existing_build_blocked(engine_factory, tmp_path,
                                               registered):
    """A registered build_id with mismatching immutable identity must be
    BLOCKED; no version/channel is created."""
    artifact = _artifact_dir(tmp_path)
    manifest = json.loads((artifact / "manifest.json").read_text())
    bad = dict(manifest, binary_sha256="0" * 64)
    _register_build(engine_factory, bad)
    with engine_factory() as session:
        report = backfill.run_backfill(session)
    assert "BLOCKED: build identity mismatch" in report["production_build"]
    assert "production_version" not in report or "created" not in report.get(
        "production_version", "")
    assert report["channel"] == (
        "BLOCKED: production version identity not verified; "
        "channel not pointed"
    )
    with engine_factory() as session:
        assert versions.get_version(session, "ce-currentfinal-20260811") is None
        assert versions.get_channel(session, "current-final") is None


def test_backfill_wrong_existing_production_version_blocked(
        engine_factory, tmp_path, registered):
    """Existing production version with wrong immutable identity => BLOCKED,
    never mutated, channel not pointed."""
    artifact = _artifact_dir(tmp_path)
    manifest = json.loads((artifact / "manifest.json").read_text())
    _register_build(engine_factory, manifest)
    with engine_factory() as session:
        v = versions.create_version_from_build(
            session, version_id="ce-currentfinal-20260811",
            display_name="Wrong", build_id=manifest["build_id"],
            command_args=["--profile", "legacy-fast"],
            status="production")
        wrong_fp = v.identity_fingerprint
        versions.set_channel(session, "current-final", "ce-currentfinal-20260811")
    with engine_factory() as session:
        report = backfill.run_backfill(session)
    assert "BLOCKED: existing ce-currentfinal-20260811 mismatches" in (
        report["production_version"])
    assert "command_args" in report["production_version"]
    assert report["channel"] == (
        "BLOCKED: production version identity not verified; "
        "channel not pointed"
    )
    with engine_factory() as session:
        fresh = versions.get_version(session, "ce-currentfinal-20260811")
        assert fresh.identity_fingerprint == wrong_fp  # never mutated
        assert list(fresh.command_args) == ["--profile", "legacy-fast"]
        # channel still points at the old (now-wrong) target
        ch = versions.get_channel(session, "current-final")
        assert ch.engine_version_id == "ce-currentfinal-20260811"


def test_backfill_historical_inconsistent_blocked(engine_factory, tmp_path,
                                                 registered):
    """Existing historical version inconsistent with the preset snapshot =>
    BLOCKED, never overwritten."""
    artifact = _artifact_dir(tmp_path)
    manifest = json.loads((artifact / "manifest.json").read_text())
    _register_build(engine_factory, manifest)
    with engine_factory() as session:
        versions.create_version_from_build(
            session, version_id="ce-currentfinal-20260811",
            display_name="Prod", build_id=manifest["build_id"],
            status="production")
        # historical with WRONG command_args
        versions.create_version_from_build(
            session, version_id="ce-currentfinal-20260806",
            display_name="Wrong historical", build_id=manifest["build_id"],
            command_args=["--profile", "wrong"], status="historical")
    with engine_factory() as session:
        report = backfill.run_backfill(session)
    assert "BLOCKED: existing ce-currentfinal-20260806 mismatches" in (
        report["historical_version"])
    assert report["channel"] == "current-final -> ce-currentfinal-20260811"


def test_backfill_channel_not_repointed_on_production_failure(
        engine_factory, tmp_path, registered):
    """When production identity validation fails, an existing channel pointing
    elsewhere is left untouched."""
    artifact = _artifact_dir(tmp_path)
    manifest = json.loads((artifact / "manifest.json").read_text())
    bad = dict(manifest, binary_sha256="0" * 64)
    _register_build(engine_factory, bad)
    with engine_factory() as session:
        session.add(EngineChannel(
            channel_id="current-final",
            engine_version_id="some-other-version",
        ))
        session.commit()
    with engine_factory() as session:
        report = backfill.run_backfill(session)
    assert "BLOCKED: build identity mismatch" in report["production_build"]
    with engine_factory() as session:
        ch = versions.get_channel(session, "current-final")
        assert ch.engine_version_id == "some-other-version"
