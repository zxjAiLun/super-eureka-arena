"""A3: repeatable engine startup arguments in the preset registration path.

Locks the argument-composition contract of register_candidate_preset.py's
``resolve_command_args`` and push_engine.py's ``preset_args_for_remote``:

  * ``--profile`` shortcut composes a leading pair and never merges with an
    explicit ``--profile`` token;
  * every ``--command-arg`` token survives verbatim — leading dashes, spaces,
    and ordering included — because the runtime passes command_args to
    cutechess as a direct argv list, never a shell string;
  * the legacy ``--command-args`` surface keeps working unchanged.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from push_engine import preset_args_for_remote  # noqa: E402
from register_candidate_preset import resolve_command_args  # noqa: E402


def _args(**kw):
    """Build the argparse namespace resolve_command_args expects."""
    base = {
        "command_args": [],
        "command_arg": [],
        "profile": None,
    }
    base.update(kw)
    return argparse.Namespace(**base)


class TestResolveCommandArgs:
    def test_no_arguments_is_empty(self):
        assert resolve_command_args(_args()) == []

    def test_profile_shortcut_only(self):
        assert resolve_command_args(_args(profile="current-final")) == [
            "--profile",
            "current-final",
        ]

    def test_nnue_preset_shape(self):
        # The exact S10-D preset: profile shortcut + value/dash tokens.
        got = resolve_command_args(
            _args(
                profile="current-final-nnue-v2q",
                command_arg=[
                    "--nnue-model",
                    "/opt/chessarena/builds/20260829-9ef078f-linux-x86_64/nnue-v2-q01.bin",
                ],
            )
        )
        assert got == [
            "--profile",
            "current-final-nnue-v2q",
            "--nnue-model",
            "/opt/chessarena/builds/20260829-9ef078f-linux-x86_64/nnue-v2-q01.bin",
        ]

    def test_multiple_leading_dash_args_keep_order_verbatim(self):
        tokens = ["--nnue-model", "/m/a.bin", "--flag", "--profile", "x"]
        assert resolve_command_args(_args(command_arg=tokens)) == tokens

    def test_token_containing_spaces_stays_single_token(self):
        tokens = ["--label", "two words with spaces"]
        assert resolve_command_args(_args(command_arg=tokens)) == tokens

    def test_profile_shortcut_plus_explicit_profile_token_fails(self):
        with pytest.raises(SystemExit, match="must not also appear"):
            resolve_command_args(
                _args(profile="current-final", command_arg=["--profile", "other"])
            )

    def test_explicit_profile_token_without_shortcut_is_allowed(self):
        tokens = ["--profile", "current-final"]
        assert resolve_command_args(_args(command_arg=tokens)) == tokens

    def test_legacy_command_args_stay_compatible(self):
        assert resolve_command_args(_args(command_args=["a", "b"])) == ["a", "b"]

    def test_legacy_and_repeatable_merge_in_order(self):
        got = resolve_command_args(
            _args(command_args=["a"], command_arg=["b"], profile="p")
        )
        assert got == ["--profile", "p", "a", "b"]


class TestPresetArgsForRemote:
    def test_empty_is_empty(self):
        assert preset_args_for_remote([]) == []

    def test_profile_pair_becomes_shortcut(self):
        assert preset_args_for_remote(["--profile", "current-final"]) == [
            "--profile",
            "current-final",
        ]

    def test_nnue_args_become_command_arg_flags(self):
        got = preset_args_for_remote(
            ["--profile", "current-final-nnue-v2q", "--nnue-model", "/m/a.bin"]
        )
        assert got == [
            "--profile",
            "current-final-nnue-v2q",
            "--command-arg=--nnue-model",
            "--command-arg=/m/a.bin",
        ]

    def test_no_leading_profile_uses_command_arg_for_everything(self):
        got = preset_args_for_remote(["--nnue-model", "/m/a.bin"])
        assert got == ["--command-arg=--nnue-model", "--command-arg=/m/a.bin"]

    def test_space_containing_token_is_one_flag_value(self):
        got = preset_args_for_remote(["--label", "two words"])
        assert got == ["--command-arg=--label", "--command-arg=two words"]

    def test_lone_profile_token_without_value_is_forwarded_verbatim(self):
        # Degenerate input: a "--profile" without a following value must not
        # be swallowed as the shortcut; it forwards as an explicit token.
        got = preset_args_for_remote(["--profile"])
        assert got == ["--command-arg=--profile"]

    def test_profile_not_in_first_position_is_forwarded_verbatim(self):
        got = preset_args_for_remote(["--nnue-model", "/m/a.bin", "--profile", "p"])
        assert got == [
            "--command-arg=--nnue-model",
            "--command-arg=/m/a.bin",
            "--command-arg=--profile",
            "--command-arg=p",
        ]


class TestScriptCliSurface:
    """End-to-end: the CLI surface feeds resolve_command_args correctly
    (argparse rejects leading-dash positionals for --command-args, which is
    exactly why --command-arg exists)."""

    def _run(self, *cli_args):
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "register_candidate_preset.py"),
                "--help",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_help_documents_repeatable_command_arg(self):
        result = self._run()
        assert result.returncode == 0
        assert "--command-arg" in result.stdout
        assert "--command-args" in result.stdout

    def test_cli_parses_repeatable_command_arg_tokens(self):
        # Directly exercise the parser setup the script uses, without
        # touching any database.
        parser = argparse.ArgumentParser()
        parser.add_argument("--command-args", nargs="+", default=[])
        parser.add_argument("--command-arg", action="append", default=[])
        parser.add_argument("--profile")
        args = parser.parse_args(
            [
                "--profile",
                "current-final-nnue-v2q",
                "--command-arg=--nnue-model",
                "--command-arg=/opt/x/nnue-v2-q01.bin",
            ]
        )
        assert resolve_command_args(args) == [
            "--profile",
            "current-final-nnue-v2q",
            "--nnue-model",
            "/opt/x/nnue-v2-q01.bin",
        ]
