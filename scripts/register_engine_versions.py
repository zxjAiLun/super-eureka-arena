#!/usr/bin/env python3
"""Idempotent EngineVersion backfill (S4.3E Phase 1).

Creates the two project EngineVersions and the initial channel:

- ce-currentfinal-20260811  production (default artifact behavior, args=[])
- ce-currentfinal-20260806  historical (snapshot of the registered
                            chessengine-production preset)
- channel current-final -> ce-currentfinal-20260811

The new production build must be registered in the Arena DB first; if
--production-build-dir is given and the build is missing, it is registered via
the canonical ``install_build.py`` path (with UCI probe). The historical
version is created from the existing registered ``chessengine-production``
preset; if that preset is absent the historical backfill is reported BLOCKED
and only the production version is created.

The script is idempotent: existing versions/channels are left untouched.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

PRODUCTION = {
    "version_id": "ce-currentfinal-20260811",
    "display_name": "CurrentFinal · LegalityFast · 2026-08-11",
    "build_id": "20260811-26604c4-linux-x86_64",
    "command_args": [],
    "uci_options": {},
    "status": "production",
}
HISTORICAL = {
    "version_id": "ce-currentfinal-20260806",
    "display_name": "CurrentFinal · 2026-08-06",
    "preset_id": "chessengine-production",
    "status": "historical",
}


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

    # Import app settings/db only after argparse (env-based config).
    from chessarena.config import get_settings
    from chessarena.db import make_engine, make_session_factory
    from chessarena.models import EngineBuild, EngineChannel, EnginePreset
    from chessarena.services import versions

    settings = get_settings()
    engine = make_engine(settings.db_url)
    session_factory = make_session_factory(engine)

    report: dict[str, str] = {}

    with session_factory() as session:
        # 1) production build must be registered
        build = (
            session.query(EngineBuild)
            .filter(EngineBuild.build_id == PRODUCTION["build_id"])
            .first()
        )
        if build is None:
            if args.production_build_dir is None:
                report["production_build"] = (
                    "BLOCKED: build not registered and no "
                    "--production-build-dir given"
                )
            else:
                manifest = json.loads(
                    (args.production_build_dir / "manifest.json").read_text()
                )
                if manifest["build_id"] != PRODUCTION["build_id"]:
                    report["production_build"] = (
                        f"BLOCKED: artifact build_id "
                        f"{manifest['build_id']} != {PRODUCTION['build_id']}"
                    )
                else:
                    install = Path(__file__).resolve().parent / "install_build.py"
                    subprocess.run(
                        [sys.executable, str(install), str(args.production_build_dir),
                         "--probe"],
                        check=True,
                    )
                    session.expire_all()
                    build = (
                        session.query(EngineBuild)
                        .filter(EngineBuild.build_id == PRODUCTION["build_id"])
                        .first()
                    )
                    report["production_build"] = (
                        "registered " + PRODUCTION["build_id"]
                    )
        else:
            report["production_build"] = "already registered"

        # 2) production version
        if build is not None:
            existing = versions.get_version(
                session, PRODUCTION["version_id"]
            )
            if existing is None:
                v = versions.create_version_from_build(
                    session,
                    version_id=PRODUCTION["version_id"],
                    display_name=PRODUCTION["display_name"],
                    build_id=PRODUCTION["build_id"],
                    command_args=PRODUCTION["command_args"],
                    uci_options=PRODUCTION["uci_options"],
                    status=PRODUCTION["status"],
                )
                report["production_version"] = (
                    f"created {v.version_id} "
                    f"fingerprint={v.identity_fingerprint}"
                )
            else:
                report["production_version"] = (
                    f"already exists {existing.version_id}"
                )

        # 3) historical version from the registered production preset
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
                    f"created {h.version_id} "
                    f"fingerprint={h.identity_fingerprint}"
                )
            else:
                report["historical_version"] = (
                    f"already exists {existing_hist.version_id}"
                )

        # 4) channel current-final -> production version
        if versions.get_version(session, PRODUCTION["version_id"]) is not None:
            ch = versions.set_channel(
                session, "current-final", PRODUCTION["version_id"]
            )
            report["channel"] = (
                f"current-final -> {ch.engine_version_id}"
            )
        else:
            report["channel"] = "BLOCKED: production version missing"

    for key, value in report.items():
        print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
