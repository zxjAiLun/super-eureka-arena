"""EngineVersion Phase 1 (S4.3E ADR): stable immutable rated-engine identity.

- ``version_id`` is the permanent Elo participant identity.
- Creation SNAPSHOTS the launch configuration (build_id, command_args,
  uci_options, source_sha, binary_sha256); later EnginePreset edits never
  affect an existing EngineVersion.
- The canonical identity fingerprint (sha256 of canonical_json{binary_sha256,
  command_args, uci_options}) is computed by the ONE shared implementation in
  ``ratings.engine_fingerprint``; EngineVersion creation and historical
  tournament matching both use it.
- Two immutable configs with different version_ids but the SAME fingerprint
  are rejected in Phase 1.
"""

from __future__ import annotations

from ..models import (
    ENGINE_VERSION_STATUSES,
    EngineBuild,
    EngineChannel,
    EnginePreset,
    EngineVersion,
    utcnow,
)
from .ratings import engine_fingerprint


class VersionError(ValueError):
    pass


def identity_fingerprint(binary_sha256: str, command_args: list, uci_options: dict) -> str:
    """Canonical immutable-config fingerprint (single shared implementation)."""
    return engine_fingerprint(
        {
            "binary_sha256": binary_sha256,
            "command_args": command_args,
            "uci_options": uci_options,
        }
    )


def _version_side(version: EngineVersion) -> dict:
    return {
        "version_id": version.version_id,
        "display_name": version.display_name,
        "build_id": version.build_id,
        "command_args": list(version.command_args or []),
        "uci_options": dict(version.uci_options or {}),
        "source_sha": version.source_sha,
        "binary_sha256": version.binary_sha256,
        "identity_fingerprint": version.identity_fingerprint,
    }


def version_to_side(version: EngineVersion) -> dict:
    """Frozen launch snapshot for tournament selection / ratings resolution."""
    return _version_side(version)


def _build_or_error(session, build_id: str) -> EngineBuild:
    build = (
        session.query(EngineBuild)
        .filter(EngineBuild.build_id == build_id)
        .first()
    )
    if build is None:
        raise VersionError(f"unknown build_id {build_id}")
    if not build.enabled:
        raise VersionError(f"build {build_id} is disabled")
    if not build.binary_sha256 or not build.git_sha:
        raise VersionError(f"build {build_id} lacks binary_sha256/git_sha")
    return build


def _validate_status(status: str) -> str:
    if status not in ENGINE_VERSION_STATUSES:
        raise VersionError(
            f"status must be one of {sorted(ENGINE_VERSION_STATUSES)}"
        )
    return status


def _commit_version(session, version: EngineVersion) -> EngineVersion:
    # Phase 1 invariant: no two versions with the same immutable fingerprint.
    duplicate = (
        session.query(EngineVersion)
        .filter(
            EngineVersion.identity_fingerprint == version.identity_fingerprint
        )
        .first()
    )
    if duplicate is not None:
        raise VersionError(
            f"identity_fingerprint already registered by "
            f"{duplicate.version_id}"
        )
    session.add(version)
    session.commit()
    return version


def create_version_from_build(
    session,
    *,
    version_id: str,
    display_name: str,
    build_id: str,
    command_args: list | None = None,
    uci_options: dict | None = None,
    status: str = "candidate",
    rating_enabled: bool = True,
    public_visible: bool = True,
) -> EngineVersion:
    """Production/default artifact mode: launch identity is the artifact's
    default behavior; source/binary identity comes from the registered build
    (never from caller-provided values)."""
    build = _build_or_error(session, build_id)
    args = list(command_args or [])
    opts = dict(uci_options or {})
    fingerprint = identity_fingerprint(build.binary_sha256, args, opts)
    version = EngineVersion(
        version_id=version_id,
        display_name=display_name,
        build_id=build.build_id,
        command_args=args,
        uci_options=opts,
        source_sha=build.git_sha,
        binary_sha256=build.binary_sha256,
        identity_fingerprint=fingerprint,
        status=_validate_status(status),
        rating_enabled=rating_enabled,
        public_visible=public_visible,
        created_at=utcnow(),
    )
    return _commit_version(session, version)


def create_version_from_preset(
    session,
    *,
    version_id: str,
    display_name: str,
    preset_id: str,
    build_id: str | None = None,
    status: str = "historical",
    rating_enabled: bool = True,
    public_visible: bool = True,
) -> EngineVersion:
    """Historical/experimental profile mode: SNAPSHOT the preset's launch
    configuration at creation; later preset edits are irrelevant."""
    preset = (
        session.query(EnginePreset)
        .filter(EnginePreset.preset_id == preset_id)
        .first()
    )
    if preset is None:
        raise VersionError(f"unknown preset_id {preset_id}")
    if not preset.enabled:
        raise VersionError(f"preset {preset_id} is disabled")
    requested = build_id or preset.build_id
    if preset.build_id != requested:
        raise VersionError(
            f"preset {preset_id} is bound to {preset.build_id}, "
            f"not {requested}"
        )
    build = _build_or_error(session, preset.build_id)
    fingerprint = identity_fingerprint(
        build.binary_sha256, preset.command_args or [], preset.uci_options or {}
    )
    version = EngineVersion(
        version_id=version_id,
        display_name=display_name,
        build_id=preset.build_id,
        command_args=list(preset.command_args or []),
        uci_options=dict(preset.uci_options or {}),
        source_sha=build.git_sha,
        binary_sha256=build.binary_sha256,
        identity_fingerprint=fingerprint,
        status=_validate_status(status),
        rating_enabled=rating_enabled,
        public_visible=public_visible,
        created_at=utcnow(),
    )
    return _commit_version(session, version)


def get_version(session, version_id: str) -> EngineVersion | None:
    return (
        session.query(EngineVersion)
        .filter(EngineVersion.version_id == version_id)
        .first()
    )


def list_versions(session) -> list[EngineVersion]:
    return (
        session.query(EngineVersion)
        .order_by(EngineVersion.created_at.asc())
        .all()
    )


def set_channel(session, channel_id: str, engine_version_id: str) -> EngineChannel:
    """Upsert a channel binding. The target version must exist; neither the
    channel nor any EngineVersion is mutated beyond the pointer."""
    version = get_version(session, engine_version_id)
    if version is None:
        raise VersionError(f"unknown engine_version_id {engine_version_id}")
    channel = (
        session.query(EngineChannel)
        .filter(EngineChannel.channel_id == channel_id)
        .first()
    )
    if channel is None:
        channel = EngineChannel(
            channel_id=channel_id,
            engine_version_id=engine_version_id,
            updated_at=utcnow(),
        )
        session.add(channel)
    else:
        channel.engine_version_id = engine_version_id
        channel.updated_at = utcnow()
    session.commit()
    return channel


def get_channel(session, channel_id: str) -> EngineChannel | None:
    return (
        session.query(EngineChannel)
        .filter(EngineChannel.channel_id == channel_id)
        .first()
    )


def list_channels(session) -> list[EngineChannel]:
    return session.query(EngineChannel).order_by(EngineChannel.channel_id).all()
