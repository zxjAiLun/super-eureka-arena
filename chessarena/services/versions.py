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

Immutability contract (V2.1, frozen): "immutable" refers to the CHESS /
LAUNCH identity only — the fields ``version_id, build_id, command_args,
uci_options, source_sha, binary_sha256, identity_fingerprint`` can never
change after creation.  The lifecycle metadata ``status, public_visible,
rating_enabled`` is mutable, but ONLY through the controlled promotion flow
(``promote_channel``) — never ad-hoc edits.  A version's identity is WHO THE
BINARY IS, not which semantic promote-commit it corresponds to.

Channel promotion contract: ``promote_channel`` performs the whole
demote-old → promote-new → repoint-channel sequence in ONE transaction; any
failure rolls back everything (no half-promotion states).  It never touches
existing frozen snapshots: tournaments froze their config at creation and
HumanGames froze their opponent snapshot at creation, so a promotion affects
only the NEXT game created through the channel.
"""

from __future__ import annotations

from ..models import (
    ENGINE_VERSION_STATUSES,
    RESULT_TERMINAL_STATUSES,
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
    rating_enabled: bool = False,
    public_visible: bool = False,
) -> EngineVersion:
    """Production/default artifact mode: launch identity is the artifact's
    default behavior; source/binary identity comes from the registered build
    (never from caller-provided values).

    V2.1 controlled lifecycle: a fresh version defaults to candidate /
    hidden / unrated; the promotion flow flips it to production / public /
    rated.  Callers that deliberately want another initial lifecycle (e.g.
    registering a KNOWN past production directly as historical) pass the
    flags explicitly.
    """
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
    status: str = "candidate",
    rating_enabled: bool = False,
    public_visible: bool = False,
) -> EngineVersion:
    """Historical/experimental profile mode: SNAPSHOT the preset's launch
    configuration at creation; later preset edits are irrelevant.

    Same controlled-lifecycle defaults as ``create_version_from_build``
    (candidate / hidden / unrated unless explicitly overridden, e.g. when
    registering a known past production directly as historical).
    """
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


# ---------------------------------------------------------------------------
# Channel promotion (V2.1)
# ---------------------------------------------------------------------------
class PromotionPlan(dict):
    """Renderable dry-run plan. Dict subkeys are JSON-stable for logging."""

    @property
    def ok(self) -> bool:
        return not self["errors"]


def _validate_target_build(session, target: EngineVersion) -> list[str]:
    """Production gate: re-validate the target's frozen provenance against
    the CURRENT EngineBuild registry.  Promotion is the only path that
    grants production status, so it re-checks:

        build exists
        build.enabled == true
        build.git_sha      == version.source_sha
        build.binary_sha256 == version.binary_sha256

    A build disabled after registration (artifact/probe/provenance issue)
    therefore blocks promotion even though the version row itself looks
    fine."""
    errors: list[str] = []
    build = (
        session.query(EngineBuild)
        .filter(EngineBuild.build_id == target.build_id)
        .first()
    )
    if build is None:
        errors.append(
            f"target {target.version_id} references unknown build "
            f"{target.build_id}"
        )
        return errors
    if not build.enabled:
        errors.append(
            f"target {target.version_id} build {target.build_id} is disabled"
        )
    if build.git_sha != target.source_sha:
        errors.append(
            f"target {target.version_id} provenance mismatch: "
            f"source_sha {target.source_sha} != registry git_sha "
            f"{build.git_sha}"
        )
    if build.binary_sha256 != target.binary_sha256:
        errors.append(
            f"target {target.version_id} provenance mismatch: "
            f"binary_sha256 {target.binary_sha256} != registry "
            f"binary_sha256 {build.binary_sha256}"
        )
    return errors


def plan_channel_promotion(
    session, channel_id: str, target_version_id: str
) -> PromotionPlan:
    """Build (but do NOT execute) a promotion plan.

    Pure read: zero DB mutation.  The plan lists every lifecycle transition
    that ``promote_channel`` would perform, plus informational impact counts.
    Informational counts never block promotion: tournaments and human games
    run on frozen snapshots, so a promotion only affects the NEXT game
    created through the channel.

    Fail-closed conditions (block promotion):
      unknown channel / unknown target / noop / target already production
      on another channel / target historical / target build disabled or
      provenance-mismatched against the CURRENT registry.
    """
    from ..models import HumanGame, Tournament

    errors: list[str] = []
    channel = get_channel(session, channel_id)
    current_version: EngineVersion | None = None
    if channel is None:
        errors.append(f"unknown channel {channel_id}")
    else:
        current_version = get_version(session, channel.engine_version_id)
        if current_version is None:
            errors.append(
                f"channel {channel_id} points at unknown version "
                f"{channel.engine_version_id}"
            )
    target = get_version(session, target_version_id)
    if target is None:
        errors.append(f"unknown engine_version_id {target_version_id}")
    elif channel is not None and current_version is not None:
        if target.version_id == current_version.version_id:
            errors.append(
                f"channel {channel_id} already points at "
                f"{target_version_id}"
            )
        if target.status == "historical":
            errors.append(
                f"target {target_version_id} is historical and cannot be "
                f"promoted"
            )
        if target.status == "production":
            errors.append(
                f"target {target_version_id} is already production on "
                f"another channel"
            )
        # Production gate: the target's build must still be registered,
        # enabled, and provenance-consistent (P1-2).
        errors.extend(_validate_target_build(session, target))

    plan = PromotionPlan(
        channel_id=channel_id,
        current=None if current_version is None else _plan_version(
            current_version
        ),
        target=None if target is None else _plan_version(target),
        after={
            "old_status": (
                "historical" if current_version is not None else None
            ),
            "target_status": "production",
            "target_public_visible": True,
            "target_rating_enabled": True,
            "channel_points_to": target_version_id,
        },
        errors=errors,
    )

    # Informational impact (never blocks): target-specific counts resolved
    # through the SAME participant resolver the ratings service uses, so a
    # legacy snapshot whose frozen fingerprint matches the target counts as
    # the target's history.
    if target is not None:
        plan["rated_history_matches_for_target"] = _count_tournaments(
            session, target.version_id,
            statuses=tuple(RESULT_TERMINAL_STATUSES),
            rated_only=True,
        )
        plan["active_tournaments_referencing_target"] = _count_tournaments(
            session, target.version_id,
            statuses=("QUEUED", "RUNNING"),
            rated_only=False,
        )
    if channel is not None and current_version is not None:
        plan["active_tournaments_referencing_current"] = _count_tournaments(
            session, current_version.version_id,
            statuses=("QUEUED", "RUNNING"),
            rated_only=False,
        )
    if channel is not None:
        plan["active_human_games_on_channel"] = (
            session.query(HumanGame)
            .filter(HumanGame.opponent_ref == f"channel:{channel_id}")
            .filter(HumanGame.status == "ACTIVE")
            .count()
        )
    return plan


def _count_tournaments(
    session,
    version_id: str,
    *,
    statuses: tuple,
    rated_only: bool,
) -> int:
    """Tournaments whose frozen sides resolve to ``version_id`` via the
    authoritative participant resolver (version_id match OR unique frozen
    fingerprint match)."""
    from ..models import Tournament
    from .ratings import resolve_participant_id

    count = 0
    rows = (
        session.query(Tournament)
        .filter(Tournament.status.in_(statuses))
        .all()
    )
    for t in rows:
        if rated_only and not t.arena_elo_enabled:
            continue
        snap = t.config_snapshot or {}
        for side_key in ("engine_a", "engine_b"):
            if resolve_participant_id(session, snap.get(side_key) or {}) \
                    == version_id:
                count += 1
                break
    return count


def _plan_version(version: EngineVersion) -> dict:
    return {
        "version_id": version.version_id,
        "display_name": version.display_name,
        "build_id": version.build_id,
        "source_sha": version.source_sha,
        "binary_sha256": version.binary_sha256,
        "identity_fingerprint": version.identity_fingerprint,
        "status": version.status,
    }


def promote_channel(
    session, channel_id: str, target_version_id: str
) -> PromotionPlan:
    """Atomically promote ``target_version_id`` on ``channel_id``.

    Single transaction:
        old production  → historical (public/rating untouched)
        target          → production + public_visible + rating_enabled
        channel         → target
    Any failure (validation, DB error) rolls back EVERYTHING — no partial
    demotion, no channel drift.  Immutable fields are never touched; callers
    can diff them byte-for-byte before/after to prove it.

    Existing tournaments/HumanGames run on frozen snapshots and are not
    affected; the promotion only changes what the channel resolves to for
    the NEXT creation.
    """
    plan = plan_channel_promotion(session, channel_id, target_version_id)
    if not plan.ok:
        raise VersionError("; ".join(plan["errors"]))

    try:
        current_version = get_version(session, plan["current"]["version_id"])
        target = get_version(session, target_version_id)
        # Demote the old production (lifecycle metadata only).
        current_version.status = "historical"
        session.add(current_version)
        # Promote the target (lifecycle metadata only).
        target.status = "production"
        target.public_visible = True
        target.rating_enabled = True
        session.add(target)
        # Repoint the channel.
        channel = get_channel(session, channel_id)
        channel.engine_version_id = target_version_id
        channel.updated_at = utcnow()
        session.add(channel)
        session.commit()
    except Exception:
        session.rollback()
        raise
    return plan
