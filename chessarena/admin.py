"""Management commands (section 21 + V2.1 engine lifecycle).

Usage:
    python -m chessarena.admin disk-usage
    python -m chessarena.admin archive-tournament <tournament_id>
    python -m chessarena.admin engine-version create \
        --build <build_id> --version <version_id> --name "<display name>" \
        [--from-preset <preset_id>] [--status candidate]
    python -m chessarena.admin engine-channel promote \
        <channel_id> <version_id> [--yes]

``engine-version create`` registers a NEW immutable EngineVersion from an
already-registered EngineBuild (default: command_args=[], uci_options={},
status=candidate, public_visible=false, rating_enabled=false — the controlled
lifecycle promotes it later) or by snapshotting an existing EnginePreset
(``--from-preset``; the preset's launch config is frozen into the version).

``engine-channel promote`` is DRY-RUN by default: it prints the full
promotion plan (current vs target, SHA identities, after-state) with ZERO
database mutation.  Pass ``--yes`` to execute the atomic promotion
(old production → historical, target → production/public/rated, channel →
target, single transaction).

These are the only supported administration operations; the worker never
deletes failed or interrupted attempt artifacts.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
import tarfile
from pathlib import Path

from .config import get_settings
from .db import make_engine, make_session_factory
from .models import Tournament
from .services import artifacts

logger = logging.getLogger("chessarena.admin")


def _disk_usage() -> int:
    run_root = artifacts.get_run_root()
    total_bytes = 0
    if run_root.exists():
        for path in run_root.rglob("*"):
            if path.is_file():
                total_bytes += path.stat().st_size
    mb = total_bytes / (1024 * 1024)
    print(f"run_root: {run_root}")
    print(f"total bytes: {total_bytes} ({mb:.1f} MiB)")
    if run_root.exists():
        tournaments = sorted(p.name for p in run_root.iterdir() if p.is_dir())
        for name in tournaments:
            size = sum(
                f.stat().st_size for f in (run_root / name).rglob("*") if f.is_file()
            )
            print(f"  {name}: {size / (1024 * 1024):.1f} MiB")
    return 0


def _archive_tournament(tournament_id: str, session_factory) -> int:
    with session_factory() as session:
        tournament = session.get(Tournament, tournament_id)
        if tournament is None:
            print(f"tournament not found: {tournament_id}", file=sys.stderr)
            return 2
        status = tournament.status
    run_dir = artifacts.tournament_run_dir(tournament_id)
    if not run_dir.exists():
        print(f"run directory not found: {tournament_id}", file=sys.stderr)
        return 2
    archive = run_dir.parent / f"{tournament_id}.tar.zst"
    print(f"archiving {run_dir} -> {archive}")
    # tar with zstd compression; fall back to gzip when zstd is unavailable.
    compressor = (
        tarfile.ZSTD_FILE_FORMAT
        if hasattr(tarfile, "ZSTD_FILE_FORMAT")
        else tarfile.GZIP_COMPRESSED
    )
    with tarfile.open(archive, "w:zst" if compressor == tarfile.ZSTD_FILE_FORMAT else "w:gz") as tf:
        tf.add(run_dir, arcname=tournament_id, recursive=True)
    size = archive.stat().st_size
    print(f"created {archive} ({size / (1024 * 1024):.1f} MiB)")
    print(f"tournament status: {status}")
    return 0


# ---------------------------------------------------------------------------
# V2.1: engine lifecycle subcommands
# ---------------------------------------------------------------------------
def _cmd_engine_version_create(args, session_factory) -> int:
    from .services import versions
    from .services.versions import VersionError

    with session_factory() as session:
        try:
            if args.from_preset:
                version = versions.create_version_from_preset(
                    session,
                    version_id=args.version,
                    display_name=args.name,
                    preset_id=args.from_preset,
                    status=args.status,
                    # Controlled lifecycle: preset-derived versions also start
                    # hidden/unrated unless promoted.
                    rating_enabled=False,
                    public_visible=False,
                )
            else:
                version = versions.create_version_from_build(
                    session,
                    version_id=args.version,
                    display_name=args.name,
                    build_id=args.build,
                    # Launch identity = the artifact's default behavior.
                    command_args=[],
                    uci_options={},
                    status=args.status,
                    rating_enabled=False,
                    public_visible=False,
                )
        except VersionError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(f"created EngineVersion {version.version_id}")
        print(f"  display_name : {version.display_name}")
        print(f"  build_id     : {version.build_id}")
        print(f"  source_sha   : {version.source_sha}")
        print(f"  binary_sha256: {version.binary_sha256}")
        print(f"  fingerprint  : {version.identity_fingerprint}")
        print(f"  command_args : {list(version.command_args or [])}")
        print(f"  uci_options  : {dict(version.uci_options or {})}")
        print(f"  status       : {version.status} "
              f"(public_visible={version.public_visible}, "
              f"rating_enabled={version.rating_enabled})")
        return 0


def _render_promotion_plan(plan) -> None:
    print("Channel")
    print(f"  {plan['channel_id']}")
    for label, key in (("CURRENT", "current"), ("TARGET", "target")):
        v = plan[key]
        if v is None:
            print(f"{label}")
            print("  (none)")
            continue
        print(f"{label}")
        print(f"  {v['version_id']}")
        print(f"  source {v['source_sha'][:16]}...")
        print(f"  binary {v['binary_sha256'][:16]}...")
        print(f"  status {v['status']}")
    after = plan["after"]
    print("AFTER")
    if plan["current"] is not None:
        print(f"  {plan['current']['version_id']} -> {after['old_status']}")
    if plan["target"] is not None:
        print(f"  {plan['target']['version_id']} -> {after['target_status']} "
              f"(public={after['target_public_visible']}, "
              f"rating={after['target_rating_enabled']})")
        print(f"  {plan['channel_id']} -> {after['channel_points_to']}")
    else:
        print("  (no valid target — nothing would change)")
    # Informational impact only: frozen snapshots are never touched.
    info = {k: v for k, v in plan.items() if k.startswith(
        ("rated_history", "active_human", "active_tournaments"))}
    for k in sorted(info):
        print(f"  ({k}: {info[k]} — informational, frozen snapshots "
              f"unaffected)")

def _cmd_engine_channel_promote(args, session_factory) -> int:
    from .services import versions
    from .services.versions import VersionError

    with session_factory() as session:
        plan = versions.plan_channel_promotion(
            session, args.channel, args.version
        )
        _render_promotion_plan(plan)
        if not plan.ok:
            print(f"error: {'; '.join(plan['errors'])}", file=sys.stderr)
            return 2
        if not args.yes:
            print()
            print("DRY RUN — no changes made. Re-run with --yes to promote.")
            return 0
        try:
            versions.promote_channel(
                session, args.channel, args.version
            )
        except VersionError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print()
        print(f"promoted: {args.channel} -> {args.version} (atomic commit)")
        return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m chessarena.admin",
        description="ChessArena management commands",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("disk-usage", help="run_root disk usage report")

    p_archive = sub.add_parser(
        "archive-tournament", help="archive a tournament run dir"
    )
    p_archive.add_argument("tournament_id")

    p_version = sub.add_parser(
        "engine-version", help="EngineVersion lifecycle"
    )
    vsub = p_version.add_subparsers(dest="engine_version_command")
    p_create = vsub.add_parser(
        "create", help="register a new immutable EngineVersion"
    )
    source = p_create.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--build", help="registered EngineBuild id (exactly one of "
                        "--build / --from-preset)"
    )
    source.add_argument(
        "--from-preset",
        help="snapshot an existing EnginePreset instead of a raw build",
    )
    p_create.add_argument("--version", required=True,
                          help="permanent version_id")
    p_create.add_argument("--name", required=True, help="display name")
    p_create.add_argument(
        "--status", default="candidate",
        choices=["candidate", "experimental"],
        help="initial lifecycle status (default: candidate)",
    )

    p_channel = sub.add_parser(
        "engine-channel", help="channel promotion"
    )
    csub = p_channel.add_subparsers(dest="engine_channel_command")
    p_promote = csub.add_parser(
        "promote", help="atomic channel promotion (dry-run by default)"
    )
    p_promote.add_argument("channel", help="channel_id, e.g. current-final")
    p_promote.add_argument("version", help="target EngineVersion id")
    p_promote.add_argument(
        "--yes", action="store_true",
        help="execute the promotion (default is a zero-mutation dry run)",
    )
    return parser


def main(argv: list[str] | None = None, settings=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if settings is None:
        settings = get_settings()
    artifacts.configure_artifact_service(settings)
    engine = make_engine(settings.db_url)
    session_factory = make_session_factory(engine)

    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "disk-usage":
        return _disk_usage()
    if args.command == "archive-tournament":
        return _archive_tournament(args.tournament_id, session_factory)
    if args.command == "engine-version":
        if args.engine_version_command == "create":
            # exactly-one --build / --from-preset enforced by the mutually
            # exclusive argparse group
            return _cmd_engine_version_create(args, session_factory)
        parser.error("engine-version requires a subcommand (create)")
    if args.command == "engine-channel":
        if args.engine_channel_command == "promote":
            return _cmd_engine_channel_promote(args, session_factory)
        parser.error("engine-channel requires a subcommand (promote)")
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
