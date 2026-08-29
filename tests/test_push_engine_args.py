"""push_engine.py CLI parsing and remote command construction.

Regression cover for two real defects found while pushing the S6-C1 build:

1. ``--command-args --profile current-final`` was rejected by argparse, because
   ``nargs="+"`` refuses a value that begins with '-'.
2. The remote call emitted one ``--command-args`` flag per value, but
   ``register_candidate_preset.py`` declares that option with ``nargs="+"``
   rather than ``action="append"``, so argparse kept ONLY the last value and
   every earlier engine argument was silently dropped.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "push_engine.py"
_spec = importlib.util.spec_from_file_location("push_engine", MODULE_PATH)
push_engine = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(push_engine)


def parse(argv: list[str]):
    return push_engine.make_parser().parse_args(argv)


BASE = ["engine.bin", "--name", "b1"]


class TestParser:
    def test_repeatable_command_arg_accepts_leading_dash(self):
        ns = parse(BASE + ["--command-arg=--profile",
                           "--command-arg=current-final"])
        assert push_engine.resolve_command_args(ns) == [
            "--profile", "current-final"]

    def test_command_arg_preserves_order(self):
        ns = parse(BASE + ["--command-arg=a", "--command-arg=b",
                           "--command-arg=c"])
        assert push_engine.resolve_command_args(ns) == ["a", "b", "c"]

    def test_legacy_command_args_still_works(self):
        ns = parse(BASE + ["--command-args", "a", "b"])
        assert push_engine.resolve_command_args(ns) == ["a", "b"]

    def test_both_spellings_concatenate_legacy_first(self):
        ns = parse(BASE + ["--command-args", "a", "--command-arg=b"])
        assert push_engine.resolve_command_args(ns) == ["a", "b"]

    def test_no_args_gives_empty_list(self):
        assert push_engine.resolve_command_args(parse(BASE)) == []

    def test_legacy_form_still_cannot_take_leading_dash(self):
        # Documents WHY --command-arg exists; argparse exits with code 2.
        with pytest.raises(SystemExit):
            parse(BASE + ["--command-args", "--profile", "current-final"])


class TestRemoteConstruction:
    def test_profile_pair_uses_the_remote_profile_shortcut(self):
        assert push_engine.preset_args_for_remote(
            ["--profile", "current-final-phase-affine"]
        ) == ["--profile", "current-final-phase-affine"]

    def test_plain_args_emit_one_command_arg_flag_per_token(self):
        """The remote script's --command-arg is action="append", so one flag
        per token is exactly right; a single --command-args list would keep
        only the last value under nargs="+"."""
        rendered = push_engine.preset_args_for_remote(["a", "b", "c"])
        assert rendered == [
            "--command-arg=a", "--command-arg=b", "--command-arg=c"]

    def test_empty_args_emit_nothing(self):
        assert push_engine.preset_args_for_remote([]) == []

    def test_leading_dash_tokens_are_forwarded_not_rejected(self):
        rendered = push_engine.preset_args_for_remote(
            ["--profile", "p", "--nnue-model", "/m/a.bin"])
        assert rendered == [
            "--profile", "p",
            "--command-arg=--nnue-model", "--command-arg=/m/a.bin"]

    def test_remote_parser_would_accept_what_we_emit(self):
        """Cross-check against register_candidate_preset.py's real parser."""
        import argparse

        remote = argparse.ArgumentParser()
        remote.add_argument("--command-args", nargs="+", default=[])
        remote.add_argument("--command-arg", action="append", default=[])
        remote.add_argument("--profile")

        ns = remote.parse_args(
            push_engine.preset_args_for_remote(["--profile", "current-final"]))
        assert ns.profile == "current-final"

        ns = remote.parse_args(push_engine.preset_args_for_remote(["a", "b"]))
        assert ns.command_arg == ["a", "b"]

        ns = remote.parse_args(push_engine.preset_args_for_remote(
            ["--profile", "p", "--nnue-model", "/m/a.bin"]))
        assert ns.profile == "p"
        assert ns.command_arg == ["--nnue-model", "/m/a.bin"]
