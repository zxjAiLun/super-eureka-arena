#!/usr/bin/env python3
"""Register a convenience EnginePreset for a freshly installed candidate build
(P4.4 F1).  Idempotent: an existing preset_id is updated in place.

The preset is "bare" by default (no command_args, empty uci_options) so it is
immediately selectable in the New Match dropdown.  Use ``--command-arg`` /
``--uci-option`` to pin engine-specific configuration.

Usage:
    python scripts/register_candidate_preset.py --build-id <id> --preset-id <id> \
        --display-name <name> [--command-arg ARG ...] [--uci-option Name=value ...]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chessarena.config import get_settings  # noqa: E402
from chessarena.db import make_engine, make_session_factory  # noqa: E402
from chessarena.models import EngineBuild, EnginePreset  # noqa: E402


def resolve_command_args(args) -> list[str]:
    """Compose the preset's command_args from the CLI surface.

    ``--profile`` is a shortcut for a leading ``["--profile", name]`` pair and
    must not be combined with an explicit ``--profile`` token; everything from
    ``--command-arg`` / legacy ``--command-args`` is appended verbatim, one
    token per element, with no shell joining or splitting.
    """
    extra = list(args.command_args) + list(args.command_arg)

    if args.profile:
        if "--profile" in extra:
            raise SystemExit(
                "error: --profile must not also appear in --command-arg"
            )
        return ["--profile", args.profile, *extra]

    return extra


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-id", required=True)
    parser.add_argument("--preset-id", required=True)
    parser.add_argument("--display-name", required=True)
    parser.add_argument("--command-args", nargs="+", default=[])
    parser.add_argument(
        "--command-arg",
        action="append",
        default=[],
        help="single startup token; repeat for each token in order "
        "(leading-dash safe, unlike --command-args)",
    )
    parser.add_argument(
        "--profile",
        help="shortcut for a leading --profile <name> pair (project engines)",
    )
    parser.add_argument("--uci-option", action="append", default=[])
    args = parser.parse_args()

    command_args = resolve_command_args(args)

    uci_options: dict = {}
    for spec in args.uci_option:
        name, _, value = spec.partition("=")
        if not name or _ == "":
            sys.exit(f"error: --uci-option must be Name=value, got {spec!r}")
        uci_options[name] = value

    engine = make_engine(get_settings().db_url)
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        build = (
            session.query(EngineBuild)
            .filter(
                EngineBuild.build_id == args.build_id,
                EngineBuild.enabled.is_(True),
                EngineBuild.uci_options_schema.isnot(None),
            )
            .first()
        )
        if build is None:
            sys.exit(
                f"error: build {args.build_id!r} is not enabled or has no UCI "
                "capability schema"
            )

        preset = (
            session.query(EnginePreset)
            .filter(EnginePreset.preset_id == args.preset_id)
            .first()
        )
        if preset is None:
            preset = EnginePreset(
                preset_id=args.preset_id,
                build_id=build.build_id,
                display_name=args.display_name,
                command_args=command_args,
                uci_options=uci_options,
                category="custom",
                public_visible=True,
                enabled=True,
            )
            session.add(preset)
            verb = "registered"
        else:
            preset.build_id = build.build_id
            preset.display_name = args.display_name
            preset.command_args = command_args
            preset.uci_options = uci_options
            preset.enabled = True
            verb = "updated"
        session.commit()

    print(f"preset {verb}: {args.preset_id} ({args.display_name}) -> build {args.build_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
