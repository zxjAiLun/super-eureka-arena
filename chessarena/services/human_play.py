"""Human-play opponent resolution and allowlist enforcement.

An opponent is referenced by one of two forms, and ONLY these forms:

    preset:<preset_id>     a registered EnginePreset (e.g. the limited-
                           strength Stockfish presets)
    channel:<channel_id>   a mutable alias that is dereferenced to ONE
                           concrete EngineVersion at game creation

The allowlist is the explicit ``ARENA_HUMAN_PLAY_OPPONENTS`` setting.  Public
visibility, preset category or version status NEVER grants human-play rights
on their own — this is a separate permission from "shown on the public site".

At game creation the selected opponent is frozen into an ``opponent_snapshot``
following the engine-version-identity ADR: the snapshot carries everything
needed to relaunch the exact same engine (build_id, binary SHA, command args,
UCI options) and is never dereferenced from mutable rows afterwards.  A later
channel promotion therefore cannot drift a game already in progress.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from sqlalchemy.orm import Session

from ..models import EngineBuild, EngineChannel, EnginePreset, EngineVersion


class OpponentError(Exception):
    """Raised when an opponent ref is malformed, unknown or not allowed."""


@dataclass(frozen=True)
class OpponentChoice:
    """Frozen launch configuration for one opponent."""

    kind: str            # "preset" | "channel"
    ref: str             # the allowlist ref the user picked
    display_name: str
    build_id: str
    binary_path: str
    binary_sha256: str
    command_args: list
    uci_options: dict
    # Provenance of the resolution (audit only; never sent to the browser).
    preset_id: Optional[str] = None
    version_id: Optional[str] = None

    def to_snapshot(self) -> dict:
        return {
            "kind": self.kind,
            "ref": self.ref,
            "display_name": self.display_name,
            "build_id": self.build_id,
            "binary_path": self.binary_path,
            "binary_sha256": self.binary_sha256,
            "command_args": list(self.command_args or []),
            "uci_options": dict(self.uci_options or {}),
            "preset_id": self.preset_id,
            "version_id": self.version_id,
        }


def _resolve_preset(session: Session, preset_id: str) -> OpponentChoice:
    preset = (
        session.query(EnginePreset)
        .filter(EnginePreset.preset_id == preset_id)
        .first()
    )
    if preset is None or not preset.enabled:
        raise OpponentError(f"unknown or disabled opponent preset: {preset_id}")
    build = (
        session.query(EngineBuild)
        .filter(EngineBuild.build_id == preset.build_id)
        .first()
    )
    if build is None or not build.enabled:
        raise OpponentError(
            f"opponent preset {preset_id} has no enabled build"
        )
    return OpponentChoice(
        kind="preset",
        ref=f"preset:{preset_id}",
        display_name=preset.display_name,
        build_id=build.build_id,
        binary_path=build.binary_path,
        binary_sha256=build.binary_sha256,
        command_args=list(preset.command_args or []),
        uci_options=dict(preset.uci_options or {}),
        preset_id=preset.preset_id,
    )


def _resolve_channel(session: Session, channel_id: str) -> OpponentChoice:
    channel = (
        session.query(EngineChannel)
        .filter(EngineChannel.channel_id == channel_id)
        .first()
    )
    if channel is None:
        raise OpponentError(f"unknown opponent channel: {channel_id}")
    version = (
        session.query(EngineVersion)
        .filter(EngineVersion.version_id == channel.engine_version_id)
        .first()
    )
    if version is None:
        raise OpponentError(
            f"opponent channel {channel_id} points at a missing version"
        )
    build = (
        session.query(EngineBuild)
        .filter(EngineBuild.build_id == version.build_id)
        .first()
    )
    if build is None or not build.enabled:
        raise OpponentError(
            f"opponent channel {channel_id} has no enabled build"
        )
    return OpponentChoice(
        kind="channel",
        ref=f"channel:{channel_id}",
        display_name=version.display_name,
        build_id=build.build_id,
        binary_path=build.binary_path,
        binary_sha256=version.binary_sha256,
        command_args=list(version.command_args or []),
        uci_options=dict(version.uci_options or {}),
        version_id=version.version_id,
    )


def resolve_opponent(session: Session, ref: str, allowed_refs) -> OpponentChoice:
    """Resolve one opponent ref, enforcing the explicit allowlist.

    ``allowed_refs`` is the parsed tuple from Settings.human_play_opponent_refs;
    a ref that is well-formed and resolvable but absent from the allowlist is
    rejected exactly like an unknown one (the error must not leak which ids
    exist).
    """
    if ref not in set(allowed_refs or ()):
        raise OpponentError("unknown opponent")
    if ref.startswith("preset:"):
        return _resolve_preset(session, ref[len("preset:"):])
    if ref.startswith("channel:"):
        return _resolve_channel(session, ref[len("channel:"):])
    raise OpponentError(f"malformed opponent ref: {ref!r}")


def _strength_label(name: str) -> str | None:
    low = name.lower()
    import re

    m = re.search(r"(\d{3,4})", low)
    if not m:
        return None
    # Only treat the number as a strength label when it looks like an Elo
    # marker in the name (e.g. "Stockfish Limited 2000").
    if "limited" in low or "elo" in low:
        return m.group(1)
    return None


def list_opponents(session: Session, allowed_refs) -> list[dict]:
    """Public opponent list (whitelisted display fields only).

    Order follows the allowlist order so the operator controls what the
    picker shows.  Unresolvable refs are skipped silently — a removed preset
    must not break the whole picker, and the list never leaks which refs
    exist beyond what is both allowed AND resolvable.
    """
    out: list[dict] = []
    for ref in allowed_refs or ():
        try:
            choice = resolve_opponent(session, ref, allowed_refs)
        except OpponentError:
            continue
        out.append(
            {
                "id": choice.ref,
                "display_name": choice.display_name,
                "kind": "stockfish" if choice.kind == "preset" else "engine",
                "strength_label": _strength_label(choice.display_name),
            }
        )
    return out
