"""S10-D0: immutable model-artifact contract (install + launch gates).

Covers:
  * install_build.validate_model_artifacts — the fail-closed install-time
    matrix (shape, path confinement, SHA, duplicates, readonly);
  * validate_launch_artifacts — the shared prelaunch/provenance gate
    (--nnue-model must point at a declared, byte-verified artifact);
  * the scheduler's per-pair prelaunch pinning (frozen snapshot list vs
    live manifest, tampered bytes, escaping and undeclared paths).
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chessarena.services.model_artifacts import (  # noqa: E402
    validate_launch_artifacts,
    validate_model_artifacts,
)

MODEL_SHA = hashlib.sha256(b"fake nnue weights").hexdigest()
OTHER_SHA = hashlib.sha256(b"other weights").hexdigest()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _make_build(tmp_path: Path, *, with_model: bool = True) -> Path:
    build_dir = tmp_path / "builds" / "b-model-1"
    build_dir.mkdir(parents=True)
    (build_dir / "engine").write_bytes(b"engine bytes")
    if with_model:
        models = build_dir / "models"
        models.mkdir()
        (models / "nnue-v2-q01.bin").write_bytes(b"fake nnue weights")
    return build_dir


def _manifest_with_models(entries: list) -> dict:
    return {"schema_version": 1, "model_artifacts": entries}


class TestValidateModelArtifacts:
    def _entry(self, **over):
        entry = {
            "model_id": "m1",
            "relative_path": "models/nnue-v2-q01.bin",
            "sha256": MODEL_SHA,
        }
        entry.update(over)
        return entry

    def test_valid_artifact_passes_and_is_normalized(self, tmp_path):
        build_dir = _make_build(tmp_path)
        out = validate_model_artifacts(
            build_dir, _manifest_with_models([self._entry()])
        )
        assert out == [
            {
                "model_id": "m1",
                "relative_path": "models/nnue-v2-q01.bin",
                "sha256": MODEL_SHA,
            }
        ]

    def test_absent_list_passes(self, tmp_path):
        build_dir = _make_build(tmp_path, with_model=False)
        assert validate_model_artifacts(build_dir, {}) == []
        assert validate_model_artifacts(build_dir, {"model_artifacts": None}) == []

    def test_non_list_rejected(self, tmp_path):
        build_dir = _make_build(tmp_path)
        with pytest.raises(SystemExit, match="must be a list"):
            validate_model_artifacts(
                build_dir, {"model_artifacts": {"model_id": "x"}}
            )

    def test_entry_not_object_rejected(self, tmp_path):
        build_dir = _make_build(tmp_path)
        with pytest.raises(SystemExit, match="must be objects"):
            validate_model_artifacts(
                build_dir, _manifest_with_models(["nope"])
            )

    def test_empty_model_id_rejected(self, tmp_path):
        build_dir = _make_build(tmp_path)
        with pytest.raises(SystemExit, match="model_id"):
            validate_model_artifacts(
                build_dir, _manifest_with_models([self._entry(model_id="")])
            )

    def test_absolute_relative_path_rejected(self, tmp_path):
        build_dir = _make_build(tmp_path)
        abs_path = str(build_dir / "models" / "nnue-v2-q01.bin")
        with pytest.raises(SystemExit, match="must be relative"):
            validate_model_artifacts(
                build_dir, _manifest_with_models(
                    [self._entry(relative_path=abs_path)]
                )
            )

    def test_dotdot_escape_rejected(self, tmp_path):
        build_dir = _make_build(tmp_path)
        (tmp_path / "outside.bin").write_bytes(b"fake nnue weights")
        with pytest.raises(SystemExit, match=r"\.\."):
            validate_model_artifacts(
                build_dir, _manifest_with_models(
                    [self._entry(relative_path="../outside.bin")]
                )
            )

    def test_missing_file_rejected(self, tmp_path):
        build_dir = _make_build(tmp_path, with_model=False)
        with pytest.raises(SystemExit, match="not a regular file"):
            validate_model_artifacts(
                build_dir,
                _manifest_with_models(
                    [self._entry(relative_path="models/absent.bin")]
                ),
            )

    def test_bad_sha_format_rejected(self, tmp_path):
        build_dir = _make_build(tmp_path)
        with pytest.raises(SystemExit, match="64 lowercase hex"):
            validate_model_artifacts(
                build_dir,
                _manifest_with_models(
                    [self._entry(sha256=MODEL_SHA.upper())]
                ),
            )

    def test_sha_mismatch_rejected(self, tmp_path):
        build_dir = _make_build(tmp_path)
        (build_dir / "models" / "nnue-v2-q01.bin").write_bytes(b"tampered!")
        with pytest.raises(SystemExit, match="SHA mismatch"):
            validate_model_artifacts(
                build_dir,
                _manifest_with_models([self._entry(sha256=OTHER_SHA)]),
            )

    def test_duplicate_model_id_rejected(self, tmp_path):
        build_dir = _make_build(tmp_path)
        with pytest.raises(SystemExit, match="duplicate model_id"):
            validate_model_artifacts(
                build_dir,
                _manifest_with_models(
                    [self._entry(), self._entry(model_id="m1")]
                ),
            )

    def test_duplicate_relative_path_rejected(self, tmp_path):
        build_dir = _make_build(tmp_path)
        with pytest.raises(SystemExit, match="duplicate model relative_path"):
            validate_model_artifacts(
                build_dir,
                _manifest_with_models(
                    [self._entry(), self._entry(model_id="m2")]
                ),
            )

    def test_valid_artifact_made_readonly(self, tmp_path):
        build_dir = _make_build(tmp_path)
        validate_model_artifacts(
            build_dir, _manifest_with_models([self._entry()])
        )
        mode = (build_dir / "models" / "nnue-v2-q01.bin").stat().st_mode
        assert not (mode & 0o222), "model file must be read-only after install"


class _FakeBuild:
    def __init__(self, build_id: str, build_dir: Path, manifest: dict):
        self.build_id = build_id
        self.binary_path = str(build_dir / "engine")
        self.manifest = manifest


class TestValidateLaunchArtifacts:
    def _build(self, tmp_path, entries=None):
        build_dir = _make_build(tmp_path)
        manifest = {"model_artifacts": entries}
        return _FakeBuild("b-model-1", build_dir, manifest)

    def _entry(self, **over):
        entry = {
            "model_id": "m1",
            "relative_path": "models/nnue-v2-q01.bin",
            "sha256": MODEL_SHA,
        }
        entry.update(over)
        return entry

    def test_no_nnue_model_flag_requires_nothing(self, tmp_path):
        build = self._build(tmp_path)
        assert validate_launch_artifacts(build, ["--profile", "current-final"]) == []
        assert validate_launch_artifacts(build, []) == []

    def test_declared_artifact_passes(self, tmp_path):
        build = self._build(tmp_path, [self._entry()])
        model_path = str(
            (Path(build.binary_path).parent / "models" / "nnue-v2-q01.bin").resolve()
        )
        assert validate_launch_artifacts(
            build, ["--profile", "p", "--nnue-model", model_path]
        ) == []

    def test_path_outside_build_rejected(self, tmp_path):
        build = self._build(tmp_path, [self._entry()])
        outside = str(tmp_path / "elsewhere.bin")
        errors = validate_launch_artifacts(
            build, ["--nnue-model", outside]
        )
        assert errors and "outside" in errors[0]

    def test_undeclared_file_inside_build_rejected(self, tmp_path):
        build = self._build(tmp_path, [self._entry()])
        build_dir = Path(build.binary_path).parent
        sneaky = build_dir / "sneaky.bin"
        sneaky.write_bytes(b"undeclared model")
        errors = validate_launch_artifacts(
            build, ["--nnue-model", str(sneaky)]
        )
        assert errors and "undeclared" in errors[0]

    def test_tampered_bytes_rejected(self, tmp_path):
        build = self._build(tmp_path, [self._entry()])
        build_dir = Path(build.binary_path).parent
        model = build_dir / "models" / "nnue-v2-q01.bin"
        model.write_bytes(b"tampered after install")
        errors = validate_launch_artifacts(
            build, ["--nnue-model", str(model)]
        )
        assert errors and "SHA mismatch" in errors[0]

    def test_flag_without_declared_artifacts_rejected(self, tmp_path):
        build = self._build(tmp_path, [])
        errors = validate_launch_artifacts(
            build, ["--nnue-model", "/any/path.bin"]
        )
        assert errors and "declares no model_artifacts" in errors[0]

    def test_dangling_flag_rejected(self, tmp_path):
        build = self._build(tmp_path, [self._entry()])
        errors = validate_launch_artifacts(build, ["--nnue-model"])
        assert errors and "requires a value" in errors[0]


class TestSchedulerPrelaunchModelGate:
    """The per-pair scheduler check: frozen snapshot list vs live manifest,
    then the shared gate re-hashes the actual bytes before Popen."""

    def _prepare(self, tmp_path, engine_factory, registered, command_args,
                 frozen_models, live_entries=None):
        """Return (scheduler, tournament_id) with a model-carrying build."""
        from chessarena.models import (
            EngineBuild,
            EnginePreset,
            PairJob,
            Tournament,
        )

        build_dir = _make_build(tmp_path)
        manifest = {
            "schema_version": 1,
            "build_id": "b-model-1",
            "engine_name": "X",
            "git_sha": "aa" * 20,
            "binary_sha256": _sha(b"engine bytes"),
            "platform": "linux-x86_64",
            "rustc_version": "1",
            "cargo_lock_sha256": "0" * 64,
            "supported_profiles": ["current-final"],
            "uci_id_name": "X",
            "uci_id_author": "t",
            "created_utc": "2026-08-30T00:00:00+00:00",
            "model_artifacts": live_entries,
        }
        (build_dir / "manifest.json").write_text(json.dumps(manifest))

        with engine_factory() as session:
            session.add(
                EngineBuild(
                    build_id="b-model-1",
                    engine_name="X",
                    git_sha=manifest["git_sha"],
                    binary_path=str(build_dir / "engine"),
                    binary_sha256=manifest["binary_sha256"],
                    platform="linux-x86_64",
                    supported_profiles=["current-final"],
                    manifest=manifest,
                    enabled=True,
                )
            )
            session.commit()

        opening_manifest = json.loads(
            (registered["opening_dir"] / "manifest.json").read_text()
        )
        model_path = str(
            (build_dir / "models" / "nnue-v2-q01.bin").resolve()
        )
        engine_a = {
            "build_id": "b-model-1",
            "profile": "current-final-nnue-v2q",
            "git_sha": manifest["git_sha"],
            "binary_sha256": manifest["binary_sha256"],
            "display_name": "A",
            "command_args": command_args,
            "uci_options": {},
            "binary_path": str(build_dir / "engine"),
            "model_artifacts": frozen_models,
        }
        engine_b = {
            "build_id": "b-model-1",
            "profile": "current-final",
            "git_sha": manifest["git_sha"],
            "binary_sha256": manifest["binary_sha256"],
            "display_name": "B",
            "command_args": ["--profile", "current-final"],
            "uci_options": {},
            "binary_path": str(build_dir / "engine"),
        }
        snapshot = {
            "engine_a": engine_a,
            "engine_b": engine_b,
            "opening_set": {
                "opening_set_id": opening_manifest["opening_set_id"],
                "sha256": opening_manifest["sha256"],
            },
            "time_control": "blitz_3_2",
            "hash_mb": 32,
            "threads": 1,
            "concurrency": 1,
            "requested_pairs": 1,
        }
        with engine_factory() as session:
            tournament = Tournament(
                name="model gate",
                status="QUEUED",
                engine_a_build_id="b-model-1",
                engine_a_profile="current-final-nnue-v2q",
                engine_b_build_id="b-model-1",
                engine_b_profile="current-final",
                opening_set_id=opening_manifest["opening_set_id"],
                time_control="blitz_3_2",
                requested_pairs=1,
                config_snapshot=snapshot,
            )
            session.add(tournament)
            session.flush()
            session.add(
                PairJob(
                    tournament_id=tournament.id,
                    pair_index=0,
                    opening_index=0,
                    status="PENDING",
                    attempt=1,
                )
            )
            session.commit()
            tid = tournament.id
        return build_dir, tid, model_path

    def _launch(self, scheduler_fixture, engine_factory, tid):
        from chessarena.models import PairJob, Tournament

        with engine_factory() as session:
            tournament = session.get(Tournament, tid)
            pair = (
                session.query(PairJob)
                .filter(PairJob.tournament_id == tid)
                .first()
            )
            run_dir = scheduler_fixture.settings.run_root / tid / "pair-0-1"
            run_dir.mkdir(parents=True, exist_ok=True)
            scheduler_fixture._prepare_and_launch(session, tournament, pair, run_dir)

    def _set_model_path(self, engine_factory, tid, model_path):
        """JSON column mutation needs reassignment (SQLAlchemy won't track
        in-place edits of a plain JSON dict)."""
        with engine_factory() as session:
            from chessarena.models import Tournament

            t = session.get(Tournament, tid)
            snap = dict(t.config_snapshot)
            side = dict(snap["engine_a"])
            args = list(side["command_args"])
            args[3] = model_path
            side["command_args"] = args
            snap["engine_a"] = side
            t.config_snapshot = snap
            session.commit()

    def test_launch_ok_with_declared_model(
        self, tmp_path, engine_factory, registered, scheduler
    ):
        entry = {
            "model_id": "m1",
            "relative_path": "models/nnue-v2-q01.bin",
            "sha256": MODEL_SHA,
        }
        args = ["--profile", "current-final-nnue-v2q", "--nnue-model", "PLACEHOLDER"]
        build_dir, tid, model_path = self._prepare(
            tmp_path, engine_factory, registered,
            args, [entry], live_entries=[entry],
        )
        # Point the snapshot's argv at the real path (frozen at creation).
        self._set_model_path(engine_factory, tid, model_path)
        self._launch(scheduler, engine_factory, tid)

    def test_launch_fails_on_tampered_model_bytes(
        self, tmp_path, engine_factory, registered, scheduler
    ):
        from chessarena.services.cutechess import CutechessLaunchError

        entry = {
            "model_id": "m1",
            "relative_path": "models/nnue-v2-q01.bin",
            "sha256": MODEL_SHA,
        }
        build_dir, tid, model_path = self._prepare(
            tmp_path, engine_factory, registered,
            ["--profile", "p", "--nnue-model", "PLACEHOLDER"],
            [entry], live_entries=[entry],
        )
        (build_dir / "models" / "nnue-v2-q01.bin").write_bytes(b"tampered")
        self._set_model_path(engine_factory, tid, model_path)
        with pytest.raises(CutechessLaunchError, match="SHA mismatch"):
            self._launch(scheduler, engine_factory, tid)

    def test_launch_fails_when_live_manifest_drifts_from_frozen(
        self, tmp_path, engine_factory, registered, scheduler
    ):
        from chessarena.services.cutechess import CutechessLaunchError

        frozen = [{
            "model_id": "m1",
            "relative_path": "models/nnue-v2-q01.bin",
            "sha256": MODEL_SHA,
        }]
        live = [{
            "model_id": "m1",
            "relative_path": "models/nnue-v2-q01.bin",
            "sha256": OTHER_SHA,
        }]
        build_dir, tid, model_path = self._prepare(
            tmp_path, engine_factory, registered,
            ["--profile", "p", "--nnue-model", "PLACEHOLDER"],
            frozen, live_entries=live,
        )
        # Make the bytes match the LIVE (drifted) manifest so the failure is
        # exactly the frozen-vs-live comparison, not the byte re-hash.
        (build_dir / "models" / "nnue-v2-q01.bin").write_bytes(b"other weights")
        self._set_model_path(engine_factory, tid, model_path)
        with pytest.raises(
            CutechessLaunchError, match="model_artifacts differ"
        ):
            self._launch(scheduler, engine_factory, tid)

    def test_launch_fails_when_nnue_model_points_outside_build(
        self, tmp_path, engine_factory, registered, scheduler
    ):
        from chessarena.services.cutechess import CutechessLaunchError

        entry = {
            "model_id": "m1",
            "relative_path": "models/nnue-v2-q01.bin",
            "sha256": MODEL_SHA,
        }
        build_dir, tid, model_path = self._prepare(
            tmp_path, engine_factory, registered,
            ["--profile", "p", "--nnue-model", "PLACEHOLDER"],
            [entry], live_entries=[entry],
        )
        outside = str(tmp_path / "outside" / "nnue.bin")
        self._set_model_path(engine_factory, tid, outside)
        with pytest.raises(CutechessLaunchError, match="outside"):
            self._launch(scheduler, engine_factory, tid)

    def test_launch_fails_on_undeclared_file_inside_build(
        self, tmp_path, engine_factory, registered, scheduler
    ):
        from chessarena.services.cutechess import CutechessLaunchError

        entry = {
            "model_id": "m1",
            "relative_path": "models/nnue-v2-q01.bin",
            "sha256": MODEL_SHA,
        }
        build_dir, tid, model_path = self._prepare(
            tmp_path, engine_factory, registered,
            ["--profile", "p", "--nnue-model", "PLACEHOLDER"],
            [entry], live_entries=[entry],
        )
        sneaky = build_dir / "sneaky.bin"
        sneaky.write_bytes(b"fake nnue weights")  # right bytes, undeclared file
        self._set_model_path(engine_factory, tid, str(sneaky))
        with pytest.raises(CutechessLaunchError, match="undeclared"):
            self._launch(scheduler, engine_factory, tid)
