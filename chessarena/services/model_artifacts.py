"""Immutable model-artifact contract for engine builds (S10-D0).

A build may ship model files (e.g. quantized NNUE networks) alongside the
engine binary.  The manifest declares them as ``model_artifacts``:

    "model_artifacts": [
      {
        "model_id": "s10-v2q-300k01",
        "relative_path": "models/nnue-v2-q01.bin",
        "sha256": "<64 lowercase hex>"
      }
    ]

The list is OPTIONAL and the schema stays version 1: builds without model
artifacts (every existing build) validate exactly as before.

There is ONE shared implementation for every consumer (install-time
validation, formal provenance gates, per-pair prelaunch re-hash); no caller
re-implements the path/SHA rules:

  * install_build.validate_model_artifacts() — install-time fail-closed
    check + make_readonly, called before the build is registered;
  * validate_launch_artifacts() — prelaunch/provenance check shared by the
    scheduler's per-pair check, version provenance validation and formal
    candidate resolution: a ``--nnue-model`` in command_args must point at
    exactly one manifest-declared artifact whose live bytes still hash to
    the declared SHA.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from .cutechess import CutechessLaunchError

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

MODEL_FLAG = "--nnue-model"


def is_sha256_hex(value) -> bool:
    return isinstance(value, str) and bool(_SHA256_RE.match(value))


def normalize_model_artifacts(manifest: dict | None) -> list[dict]:
    """Return the manifest's model_artifacts list ([] when absent)."""
    if not manifest:
        return []
    artifacts = (manifest or {}).get("model_artifacts")
    if artifacts is None:
        return []
    return list(artifacts)


def validate_model_artifacts(build_dir: Path, manifest: dict) -> list[dict]:
    """Install-time fail-closed validation of every declared model artifact.

    Raises SystemExit (the install script's error channel) on the first
    violation; on success returns the normalized list and every artifact
    file is made read-only.
    """
    artifacts = manifest.get("model_artifacts")
    if artifacts is None:
        return []
    if not isinstance(artifacts, list):
        raise SystemExit("error: model_artifacts must be a list")

    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    normalized: list[dict] = []
    for entry in artifacts:
        if not isinstance(entry, dict):
            raise SystemExit("error: model_artifacts entries must be objects")
        model_id = entry.get("model_id")
        rel = entry.get("relative_path")
        sha = entry.get("sha256")
        if not isinstance(model_id, str) or not model_id.strip():
            raise SystemExit(f"error: model artifact model_id must be non-empty (got {model_id!r})")
        if model_id in seen_ids:
            raise SystemExit(f"error: duplicate model_id {model_id!r}")
        seen_ids.add(model_id)
        if not isinstance(rel, str) or not rel:
            raise SystemExit(f"error: model artifact {model_id!r} relative_path must be non-empty")
        if rel in seen_paths:
            raise SystemExit(f"error: duplicate model relative_path {rel!r}")
        seen_paths.add(rel)
        rel_path = Path(rel)
        if rel_path.is_absolute():
            raise SystemExit(
                f"error: model artifact {model_id!r} relative_path must be relative (got {rel!r})"
            )
        if ".." in rel_path.parts:
            raise SystemExit(
                f"error: model artifact {model_id!r} relative_path must not contain '..' (got {rel!r})"
            )
        if not is_sha256_hex(sha):
            raise SystemExit(
                f"error: model artifact {model_id!r} sha256 must be 64 lowercase hex (got {sha!r})"
            )
        target = (build_dir / rel_path).resolve()
        build_resolved = build_dir.resolve()
        if build_resolved != target and build_resolved not in target.parents:
            raise SystemExit(
                f"error: model artifact {model_id!r} resolves outside the build directory: {rel!r}"
            )
        if not target.is_file():
            raise SystemExit(f"error: model artifact {model_id!r} is not a regular file: {target}")
        import hashlib

        h = hashlib.sha256()
        with open(target, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        actual = h.hexdigest()
        if actual != sha:
            raise SystemExit(
                f"error: model artifact {model_id!r} SHA mismatch: manifest {sha} actual {actual}"
            )
        normalized.append(
            {
                "model_id": model_id,
                "relative_path": rel,
                "sha256": sha,
            }
        )

    import os
    import stat

    for entry in normalized:
        target = build_dir / entry["relative_path"]
        current = stat.S_IMODE(target.stat().st_mode)
        os.chmod(target, current & ~stat.S_IWUSR & ~stat.S_IWGRP & ~stat.S_IWOTH)

    return normalized


def _artifact_paths(build) -> dict[str, dict]:
    """Map resolved absolute path -> artifact entry for one build row."""
    out: dict[str, dict] = {}
    for entry in normalize_model_artifacts(build.manifest if hasattr(build, "manifest") else None):
        base = Path(build.binary_path).parent
        out[str((base / entry["relative_path"]).resolve())] = entry
    return out


def validate_launch_artifacts(build, command_args) -> list[str]:
    """Shared prelaunch/provenance gate for model artifacts.

    * ``command_args`` without ``--nnue-model``: no model requirement.
    * with ``--nnue-model <path>``: the path must resolve to exactly one
      artifact declared in the build's manifest, and the live file must
      still hash to the declared SHA-256.

    Returns a list of human-readable errors (empty = valid); callers map
    this to CutechessLaunchError (scheduler, fail-closed before Popen) or
    append it to provenance error lists (versions / formal experiments).
    """
    args = list(command_args or [])
    errors: list[str] = []
    if MODEL_FLAG not in args:
        return errors

    declared = _artifact_paths(build)
    if not declared:
        return [
            f"build {build.build_id} declares no model_artifacts but "
            f"command_args reference {MODEL_FLAG}"
        ]

    build_dir = Path(build.binary_path).parent
    for i, token in enumerate(args):
        if token != MODEL_FLAG:
            continue
        if i + 1 >= len(args):
            errors.append(f"{MODEL_FLAG} requires a value in command_args")
            continue
        raw = args[i + 1]
        resolved = str(Path(raw).resolve())
        entry = declared.get(resolved)
        if entry is None:
            inside = str(build_dir.resolve()) in resolved or resolved.startswith(
                str(build_dir.resolve()) + os.sep
            )
            if inside:
                errors.append(
                    f"{MODEL_FLAG} points at an undeclared file inside the "
                    f"build: {raw}"
                )
            else:
                errors.append(
                    f"{MODEL_FLAG} points outside the build's declared "
                    f"model artifacts: {raw}"
                )
            continue
        path = Path(resolved)
        if not path.is_file():
            errors.append(
                f"declared model artifact {entry['model_id']} missing: {path}"
            )
            continue
        from .artifacts import sha256_file

        actual = sha256_file(path)
        if actual != entry["sha256"]:
            errors.append(
                f"model artifact {entry['model_id']} SHA mismatch: expected "
                f"{entry['sha256']} got {actual}"
            )
    return errors
