"""EngineVersion / EngineChannel API (S4.3E Phase 1 + V2.1 lifecycle).

EngineVersion is the permanent immutable rated-engine identity
(version == Elo participant). Creation snapshots the launch configuration;
there is NO generic update endpoint for build/launch identity fields.

V2.1 controlled lifecycle on the HTTP surface:
- ``POST /engine-versions`` mints ONLY candidate/experimental versions,
  always hidden and unrated — production/historical/public/rated are
  reachable solely through the promotion flow.
- ``PUT /engine-channels/{id}`` (generic repoint) is REMOVED: it could
  bypass the lifecycle contract and create half-promotion states. The
  controlled surface is ``POST /engine-channels/{id}/promote``, which runs
  the atomic ``promote_channel`` (old production -> historical, target ->
  production/public/rated, channel repoint — one transaction).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import (
    RESULT_TERMINAL_STATUSES,
    EngineBuild,
    EngineChannel,
    EngineVersion,
    Tournament,
)
from ..schemas import (
    EngineChannelOut,
    EngineChannelUpdate,
    EngineVersionCreate,
    EngineVersionOut,
)
from ..security import require_same_origin
from ..services.versions import (
    VersionError,
    create_version_from_build,
    create_version_from_preset,
    get_channel,
    get_version,
    list_channels,
    list_versions,
    plan_channel_promotion,
    promote_channel,
)

router = APIRouter(tags=["engine-versions"])
admin_router = APIRouter(tags=["admin"], include_in_schema=False)


@router.get("/engine-versions", response_model=list[EngineVersionOut])
def list_versions_endpoint(session: Session = Depends(get_db)):
    return list_versions(session)


@router.get("/engine-versions/{version_id}", response_model=EngineVersionOut)
def get_version_endpoint(version_id: str, session: Session = Depends(get_db)):
    version = get_version(session, version_id)
    if version is None:
        raise HTTPException(status_code=404, detail="engine version not found")
    return version


@router.post(
    "/engine-versions",
    response_model=EngineVersionOut,
    dependencies=[Depends(require_same_origin)],
)
def create_version_endpoint(
    body: EngineVersionCreate,
    session: Session = Depends(get_db),
):
    if (body.build_id is None) == (body.preset_id is None):
        raise HTTPException(
            status_code=422,
            detail="exactly one of build_id or preset_id is required",
        )
    try:
        if body.build_id is not None:
            return create_version_from_build(
                session,
                version_id=body.version_id,
                display_name=body.display_name,
                build_id=body.build_id,
                command_args=body.command_args,
                uci_options=body.uci_options,
                # Controlled lifecycle: the HTTP surface can only mint
                # candidate/experimental, always hidden and unrated.
                status=body.status,
                rating_enabled=False,
                public_visible=False,
            )
        return create_version_from_preset(
            session,
            version_id=body.version_id,
            display_name=body.display_name,
            preset_id=body.preset_id,
            status=body.status,
            rating_enabled=False,
            public_visible=False,
        )
    except VersionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/engine-channels", response_model=list[EngineChannelOut])
def list_channels_endpoint(session: Session = Depends(get_db)):
    return list_channels(session)


@router.put(
    "/engine-channels/{channel_id}",
    response_model=EngineChannelOut,
    dependencies=[Depends(require_same_origin)],
    deprecated=True,
)
def set_channel_endpoint(
    channel_id: str,
    body: EngineChannelUpdate,
    session: Session = Depends(get_db),
):
    """Generic channel repoint — REMOVED from the operator surface (V2.1).

    It bypassed the controlled lifecycle: pointing a channel at a candidate
    while the old version stayed "production" created exactly the
    half-promotion state the lifecycle contract forbids. The replacement is
    ``POST /engine-channels/{channel_id}/promote``. ``set_channel`` remains
    an internal service for bootstrap/backfill only.
    """
    raise HTTPException(
        status_code=405,
        detail=(
            "generic channel repoint is not permitted; use "
            "POST /engine-channels/{channel_id}/promote for the controlled "
            "promotion flow"
        ),
    )


@router.post(
    "/engine-channels/{channel_id}/promote",
    response_model=EngineChannelOut,
    dependencies=[Depends(require_same_origin)],
)
def promote_channel_endpoint(
    channel_id: str,
    body: EngineChannelUpdate,
    session: Session = Depends(get_db),
):
    """Controlled promotion: run the atomic ``promote_channel`` flow.

    ``engine_version_id`` is the TARGET. One transaction: old production ->
    historical, target -> production + public + rated, channel -> target.
    Existing tournaments/HumanGames run on frozen snapshots and are never
    touched.
    """
    try:
        promote_channel(session, channel_id, body.engine_version_id)
    except VersionError as exc:
        # Surface the full dry-run plan errors for the operator.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    channel = get_channel(session, channel_id)
    assert channel is not None  # promote_channel validated its existence
    return channel


# ---------------------------------------------------------------------------
# Admin HTML pages (read-only: immutable identity has no edit surface)
# ---------------------------------------------------------------------------
@admin_router.get("/admin/versions/", response_class=HTMLResponse)
def admin_versions_list(request: Request, session: Session = Depends(get_db)):
    versions = list_versions(session)
    channels = list_channels(session)
    channel_map: dict[str, list[str]] = {}
    for c in channels:
        channel_map.setdefault(c.engine_version_id, []).append(c.channel_id)
    rows = []
    for v in versions:
        build = (
            session.query(EngineBuild)
            .filter(EngineBuild.build_id == v.build_id)
            .first()
        )
        rows.append(
            {
                "version_id": v.version_id,
                "display_name": v.display_name,
                "status": v.status,
                "build_id": v.build_id,
                "source_sha": v.source_sha,
                "binary_sha256": v.binary_sha256,
                "command_args": list(v.command_args or []),
                "created_at": v.created_at,
                "rating_enabled": v.rating_enabled,
                "public_visible": v.public_visible,
                "channels": channel_map.get(v.version_id, []),
                "build_enabled": bool(build and build.enabled),
            }
        )
    return request.app.state.templates.TemplateResponse(
        request,
        "admin_versions.html",
        {"versions": rows, "settings": request.app.state.settings},
    )


@admin_router.get("/admin/versions/{version_id}",
                  response_class=HTMLResponse)
def admin_version_detail(
    version_id: str, request: Request, session: Session = Depends(get_db)
):
    from ..services.ratings import compute_ratings, resolve_participant_id

    version = get_version(session, version_id)
    if version is None:
        raise HTTPException(status_code=404, detail="engine version not found")
    build = (
        session.query(EngineBuild)
        .filter(EngineBuild.build_id == version.build_id)
        .first()
    )
    channels = [
        c.channel_id
        for c in list_channels(session)
        if c.engine_version_id == version_id
    ]
    # Current ratings by time-control pool.
    all_ratings = compute_ratings(session)
    ratings_rows = []
    for tc, pool in all_ratings.items():
        for row in pool["engines"]:
            if row.get("participant_id") == version_id:
                ratings_rows.append({"tc": tc, **row})
    # Match history: any result-terminal tournament whose snapshot side
    # carries this version_id.
    matches = (
        session.query(Tournament)
        .filter(Tournament.status.in_(RESULT_TERMINAL_STATUSES))
        .all()
    )
    history = []
    for t in matches:
        snap = t.config_snapshot or {}
        # Use the SAME authoritative resolver as the ratings service: a
        # legacy snapshot whose frozen fingerprint uniquely matches this
        # EngineVersion counts as this version's history, exactly like it
        # counts toward its Elo/Games/W-D-L.
        used = (
            resolve_participant_id(session, snap.get("engine_a") or {})
            == version_id
            or resolve_participant_id(session, snap.get("engine_b") or {})
            == version_id
        )
        if used:
            history.append(t)
    history.sort(key=lambda t: t.finished_at or t.created_at, reverse=True)
    return request.app.state.templates.TemplateResponse(
        request,
        "admin_version_detail.html",
        {
            "version": version,
            "build": build,
            "channels": channels,
            "ratings_rows": ratings_rows,
            "history": history,
            "settings": request.app.state.settings,
        },
    )
