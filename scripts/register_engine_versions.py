#!/usr/bin/env python3
"""Idempotent EngineVersion backfill (S4.3E Phase 1) — fail-closed.

Creates / verifies the two project EngineVersions and the initial channel:

- ce-currentfinal-20260811  production (default artifact behavior, args=[])
- ce-currentfinal-20260806  historical (snapshot of the registered
                            chessengine-production preset)
- channel current-final -> ce-currentfinal-20260811

Idempotence is FAIL-CLOSED: an existing version with the declared version_id
but a mismatching immutable identity (build_id / source_sha / binary_sha256 /
command_args / uci_options / identity_fingerprint) is BLOCKED and never
mutated or silently accepted; the channel is only pointed after the
production version passes exact identity verification.

The new production build must be registered in the Arena DB first; if
--production-build-dir is given and the build is missing, it is registered via
the canonical ``install_build.py`` path (with UCI probe).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

PRODUCTION = {
    "version_id": "ce-currentfinal-20260811",
    "display_name": "CurrentFinal · LegalityFast · 2026-08-11",
    "build_id": "20260811-26604c4-linux-x86_64",
    "source_sha": "26604c425625d69e5b7e7b967db8926f4da01b8a",
    "binary_sha256": "f0e8f91a3a0828a158672cecdf7859dbd9a3c9bac36b965bdcc90db31b51189d",
    "command_args": [],
    "uci_options": {},
    "identity_fingerprint": (
        "d05b42b1af91560a54be891ac5cfa880ded570a08b8e0178a0449e797f48c13c"
    ),
}
HISTORICAL = {
    "version_id": "ce-currentfinal-20260806",
    "display_name": "CurrentFinal · 2026-08-06",
    "preset_id": "chessengine-production",
    "status": "historical",
}

# Immutable fields verified for an existing EngineVersion.
IMMUTABLE_FIELDS = (
    "version_id",
    "build_id",
    "source_sha",
    "binary_sha256",
    "command_args",
    "uci_options",
    "identity_fingerprint",
)


def _read_manifest(build_dir: Path) -> dict:
    return json.loads((build_dir / "manifest.json").read_text(encoding="utf-8"))


def _version_mismatches(version, expected: dict) -> list[str]:
    """Compare an existing EngineVersion against the declared immutable
    identity; returns a list of mismatched field names (empty == exact)."""
    mismatches = []
    if version.build_id != expected["build_id"]:
        mismatches.append("build_id")
    if version.source_sha != expected["source_sha"]:
        mismatches.append("source_sha")
    if version.binary_sha256 != expected["binary_sha256"]:
        mismatches.append("binary_sha256")
    if list(version.command_args or []) != list(expected["command_args"]):
        mismatches.append("command_args")
    if dict(version.uci_options or {}) != dict(expected["uci_options"]):
        mismatches.append("uci_options")
    if version.identity_fingerprint != expected["identity_fingerprint"]:
        mismatches.append("identity_fingerprint")
    return mismatches


def run_backfill(session, production_build_dir: Path | None = None) -> dict:
    """Idempotent, fail-closed backfill. Returns a report dict; never mutates
    an existing EngineVersion on a mismatch."""
    from chessarena.models import EngineBuild, EnginePreset
    from chessarena.services import versions

    report: dict[str, str] = {}

    # 1) production build: register if missing, else verify exact identity.
    build = (
        session.query(EngineBuild)
        .filter(EngineBuild.build_id == PRODUCTION["build_id"])
        .first()
    )
    if build is None:
        if production_build_dir is None:
            report["production_build"] = (
                "BLOCKED: build not registered and no --production-build-dir given"
            )
        else:
            manifest = _read_manifest(production_build_dir)
            if manifest["build_id"] != PRODUCTION["build_id"]:
                report["production_build"] = (
                    f"BLOCKED: artifact build_id {manifest['build_id']} != "
                    f"{PRODUCTION['build_id']}"
                )
            else:
                install = Path(__file__).resolve().parent / "install_build.py"
                subprocess.run(
                    [sys.executable, str(install), str(production_build_dir),
                     "--probe"],
                    check=True,
                )
                session.expire_all()
                build = (
                    session.query(EngineBuild)
                    .filter(EngineBuild.build_id == PRODUCTION["build_id"])
                    .first()
                )
                # Fresh-install MUST re-verify the registered build against
                # the frozen expected identity: install_build.py only proves
                # the binary matches ITS OWN manifest, never that this is the
                # identity ce-currentfinal-20260811 was declared with.
                if (
                    build is None
                    or build.git_sha != PRODUCTION["source_sha"]
                    or build.binary_sha256 != PRODUCTION["binary_sha256"]
                ):
                    report["production_build"] = (
                        "BLOCKED: freshly registered build does not match "
                        f"the frozen identity (git={build and build.git_sha}, "
                        f"binary={build and build.binary_sha256[:16]}...)"
                    )
                    build = None  # unusable for this backfill
                else:
                    report["production_build"] = (
                        "registered and verified exact "
                        + PRODUCTION["build_id"]
                    )
    else:
        if (
            build.git_sha != PRODUCTION["source_sha"]
            or build.binary_sha256 != PRODUCTION["binary_sha256"]
        ):
            report["production_build"] = (
                f"BLOCKED: build identity mismatch "
                f"(git={build.git_sha}, binary={build.binary_sha256[:16]}...)"
            )
            build = None  # unusable for this backfill
        else:
            report["production_build"] = "verified exact"

    # 2) production version (create or verify exact identity).
    production_ok = False
    if build is not None:
        existing = versions.get_version(session, PRODUCTION["version_id"])
        if existing is None:
            v = versions.create_version_from_build(
                session,
                version_id=PRODUCTION["version_id"],
                display_name=PRODUCTION["display_name"],
                build_id=PRODUCTION["build_id"],
                command_args=PRODUCTION["command_args"],
                uci_options=PRODUCTION["uci_options"],
                status="production",
            )
            report["production_version"] = (
                f"created {v.version_id} fingerprint={v.identity_fingerprint}"
            )
            production_ok = True
        else:
            mismatches = _version_mismatches(existing, PRODUCTION)
            if mismatches:
                report["production_version"] = (
                    f"BLOCKED: existing {existing.version_id} mismatches "
                    f"immutable identity on {', '.join(mismatches)}"
                )
            else:
                report["production_version"] = (
                    f"already exists and verified exact ({existing.version_id})"
                )
                production_ok = True

    # 3) historical version: derive expected identity from the registered
    #    preset + its build, then create or verify.
    preset = (
        session.query(EnginePreset)
        .filter(EnginePreset.preset_id == HISTORICAL["preset_id"])
        .first()
    )
    if preset is None:
        report["historical_version"] = (
            "BLOCKED: preset chessengine-production not registered"
        )
    else:
        hist_build = (
            session.query(EngineBuild)
            .filter(EngineBuild.build_id == preset.build_id)
            .first()
        )
        if hist_build is None or not hist_build.enabled:
            report["historical_version"] = (
                f"BLOCKED: preset build {preset.build_id} missing/disabled"
            )
        else:
            expected_hist = {
                "version_id": HISTORICAL["version_id"],
                "build_id": preset.build_id,
                "source_sha": hist_build.git_sha,
                "binary_sha256": hist_build.binary_sha256,
                "command_args": list(preset.command_args or []),
                "uci_options": dict(preset.uci_options or {}),
                "identity_fingerprint": versions.identity_fingerprint(
                    hist_build.binary_sha256,
                    preset.command_args or [],
                    preset.uci_options or {},
                ),
            }
            existing_hist = versions.get_version(
                session, HISTORICAL["version_id"]
            )
            if existing_hist is None:
                h = versions.create_version_from_preset(
                    session,
                    version_id=HISTORICAL["version_id"],
                    display_name=HISTORICAL["display_name"],
                    preset_id=HISTORICAL["preset_id"],
                    status=HISTORICAL["status"],
                )
                report["historical_version"] = (
                    f"created {h.version_id} fingerprint={h.identity_fingerprint}"
                )
            else:
                mismatches = _version_mismatches(existing_hist, expected_hist)
                if mismatches:
                    report["historical_version"] = (
                        f"BLOCKED: existing {existing_hist.version_id} mismatches "
                        f"derived preset identity on {', '.join(mismatches)}"
                    )
                else:
                    report["historical_version"] = (
                        f"already exists and verified exact "
                        f"({existing_hist.version_id})"
                    )

    # 4) channel: only after the production version passed exact verification.
    if production_ok:
        ch = versions.set_channel(
            session, "current-final", PRODUCTION["version_id"]
        )
        report["channel"] = f"current-final -> {ch.engine_version_id}"
    else:
        report["channel"] = (
            "BLOCKED: production version identity not verified; "
            "channel not pointed"
        )

    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--production-build-dir",
        type=Path,
        default=None,
        help="local artifact dir (engine + manifest.json) to register the "
             "production build if it is missing",
    )
    args = parser.parse_args()

    from chessarena.config import get_settings
    from chessarena.db import make_engine, make_session_factory

    settings = get_settings()
    engine = make_engine(settings.db_url)
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        report = run_backfill(session, args.production_build_dir)
    for key, value in report.items():
        print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
