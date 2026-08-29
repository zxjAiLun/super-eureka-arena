#!/usr/bin/env python3
"""Push a newly built UCI engine binary to the Arena server and register it.

One command turns a local engine binary into a selectable Arena engine:

    python scripts/push_engine.py E:\\...\\chess-engine-demo.exe --name candidate-20260809

It:
  1. computes the binary SHA-256 locally (you never handle SHAs),
  2. uploads the binary to the server,
  3. stages it under the builds tree,
  4. runs a real UCI probe + registers an immutable EngineBuild
     (scripts/install_external_build.py), and
  5. registers a convenience EnginePreset so the build appears in the
     New Match dropdown (scripts/register_candidate_preset.py).

Requires passwordless ``ssh <host>`` / ``scp <host>`` (e.g. the ``server1``
host in ~/.ssh/config).  Only the deploy/chessarena paths on the server are
touched, via the same sudo boundary the deploy user uses.

Usage:
    python scripts/push_engine.py <local-binary> --name <build/preset id>
        [--host server1] [--platform linux-x86_64]
        [--command-args ARG ...] [--uci-option Name=value ...]
"""

from __future__ import annotations

import argparse
import hashlib
import shlex
import subprocess
import sys
from pathlib import Path

VENV_PY = "/opt/chessarena/venv/bin/python"
SCRIPTS_DIR = "/opt/chessarena/app/current/scripts"
BUILDS_DIR = "/opt/chessarena/builds"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def run(argv: list[str]) -> None:
    print("+ " + " ".join(shlex.quote(a) for a in argv), file=sys.stderr)
    subprocess.run(argv, check=True)


def preset_args_for_remote(command_args: list[str]) -> list[str]:
    """Translate a preset's command_args into register_candidate_preset.py
    CLI arguments.

    A leading ``--profile <name>`` pair (the historical shape produced by the
    old ``--profile`` shortcut) is forwarded as the script's ``--profile``
    shortcut; every remaining token becomes one ``--command-arg=<token>``
    flag.  This never relies on argparse accepting a bare leading-dash token
    as a positional, which is what made the previous inline expansion
    silently wrong for anything beyond a single profile pair.
    """
    if not command_args:
        return []

    out: list[str] = []

    if len(command_args) >= 2 and command_args[0] == "--profile":
        out += ["--profile", command_args[1]]
        command_args = command_args[2:]

    for arg in command_args:
        out.append(f"--command-arg={arg}")

    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("--name", required=True, help="build id / preset id, e.g. candidate-20260809")
    parser.add_argument("--host", default="server1")
    parser.add_argument("--platform", default="linux-x86_64")
    parser.add_argument("--command-args", nargs="+", default=[])
    parser.add_argument("--uci-option", action="append", default=[])
    args = parser.parse_args()

    binary = args.binary.resolve()
    if not binary.is_file():
        sys.exit(f"error: binary not found: {binary}")

    sha = sha256_file(binary)
    build_id = args.name
    build_dir = f"{BUILDS_DIR}/{build_id}"
    preset_argv = preset_args_for_remote(args.command_args)

    # 1. Upload the binary to a staging path on the server.
    run(["scp", str(binary), f"{args.host}:/tmp/engine-push-{build_id}"])

    # 2. Stage + install + register on the server (single ssh, args quoted).
    remote = " && ".join(
        [
            f"sudo mkdir -p {shlex.quote(build_dir)}",
            f"sudo mv {shlex.quote('/tmp/engine-push-' + build_id)} "
            f"{shlex.quote(build_dir + '/engine')}",
            f"sudo chown -R chessarena:chessarena {shlex.quote(build_dir)}",
            f"sudo chmod +x {shlex.quote(build_dir + '/engine')}",
            " ".join(
                [
                    f"sudo -u chessarena {shlex.quote(VENV_PY)}",
                    shlex.quote(f"{SCRIPTS_DIR}/install_external_build.py"),
                    shlex.quote(build_dir),
                    f"--build-id {shlex.quote(build_id)}",
                    f"--engine-name {shlex.quote(build_id)}",
                    f"--binary-sha256 {shlex.quote(sha)}",
                    f"--platform {shlex.quote(args.platform)}",
                ]
            ),
            " ".join(
                [
                    f"sudo -u chessarena {shlex.quote(VENV_PY)}",
                    shlex.quote(f"{SCRIPTS_DIR}/register_candidate_preset.py"),
                    f"--build-id {shlex.quote(build_id)}",
                    f"--preset-id {shlex.quote(build_id)}",
                    f"--display-name {shlex.quote(build_id)}",
                ]
                + preset_argv
                + [f"--uci-option {shlex.quote(o)}" for o in args.uci_option]
            ),
        ]
    )
    run(["ssh", args.host, remote])

    print()
    print(f"Registered: {build_id}")
    print("Ready for match creation.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
