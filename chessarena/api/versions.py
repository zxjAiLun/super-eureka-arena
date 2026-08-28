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
from fastapi.responses import HTMLResponse, RedirectResponse
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
from ..security import require_same_origin, validate_csrf_token
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
# Admin HTML pages (V2.1-B2: timeline + guarded promotion UI)
# ---------------------------------------------------------------------------
@admin_router.get("/admin/versions/", response_class=HTMLResponse)
def admin_versions_list(request: Request, session: Session = Depends(get_db)):
    """EngineVersion TIMELINE: current production first, then history in
    creation order, plus channel badges resolved live from EngineChannel.
    Data comes only from EngineVersion + EngineChannel — promoting a new
    version updates the timeline automatically. Promotes that left no
    surviving immutable artifact are intentionally NOT nodes (the design
    doc narrates them)."""
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
    production = [r for r in rows if r["status"] == "production"]
    # The CurrentFinal lineage: ONLY historical versions belong here —
    # candidates/experiments are pre-lineage and get their own section, so
    # the timeline never fills with failed/rejected experiments.
    # list_versions is created_at-asc, which IS the oldest -> newest order
    # the lineage should read in (0806 -> 0811 -> ...).
    history = [r for r in rows if r["status"] == "historical"]
    # Pending work reads best newest-first.
    pending = [
        r for r in reversed(rows)
        if r["status"] in ("candidate", "experimental")
    ]
    return request.app.state.templates.TemplateResponse(
        request,
        "admin_versions.html",
        {
            "production": production,
            "history": history,
            "pending": pending,
            "settings": request.app.state.settings,
        },
    )


@admin_router.get("/admin/versions/{version_id}",
                  response_class=HTMLResponse)
def admin_version_detail(
    version_id: str, request: Request, session: Session = Depends(get_db),
    promoted: str | None = None,
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
    # V2.1-B2: the promotion entry point is offered ONLY for versions that
    # could possibly pass the gate (candidate/experimental with the default
    # launch identity). The REAL decision always comes from
    # plan_channel_promotion on the preview page — this flag never
    # authorizes anything by itself.
    promoteable_entry = (
        version.status in ("candidate", "experimental")
        and not list(version.command_args or [])
        and not dict(version.uci_options or {})
    )
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
            "promoteable_entry": promoteable_entry,
            "promoted": promoted,
            "settings": request.app.state.settings,
        },
    )


# ---------------------------------------------------------------------------
# V2.1-B2: guarded promotion confirmation UI
# ---------------------------------------------------------------------------
@admin_router.get(
    "/admin/versions/{version_id}/promote/{channel_id}",
    response_class=HTMLResponse,
)
def admin_version_promote_preview(
    version_id: str, channel_id: str, request: Request,
    session: Session = Depends(get_db),
):
    """PURE PREVIEW: renders plan_channel_promotion() with zero mutation.

    Blocked plans show every error and NO confirm button; only a clean plan
    offers the confirm form. The POST re-runs the whole gate at submission
    time — this page's plan is never trusted as input.
    """
    version = get_version(session, version_id)
    if version is None:
        raise HTTPException(status_code=404, detail="engine version not found")
    plan = plan_channel_promotion(session, channel_id, version_id)
    return request.app.state.templates.TemplateResponse(
        request,
        "admin_version_promote.html",
        {
            "plan": dict(plan),
            "version": version,
            "csrf_token": getattr(request.state, "csrf_token", ""),
            "settings": request.app.state.settings,
        },
    )


@admin_router.post(
    "/admin/versions/{version_id}/promote/{channel_id}",
    response_class=RedirectResponse,
    dependencies=[Depends(require_same_origin)],
)
async def admin_version_promote_confirm(
    version_id: str, channel_id: str, request: Request,
    session: Session = Depends(get_db),
):
    """Re-run the ENTIRE production gate at submission time via
    promote_channel() — the GET page's plan is never trusted (the build may
    have been disabled or the registry drifted between GET and POST)."""
    form = dict(await request.form())
    validate_csrf_token(request, form)
    try:
        promote_channel(session, channel_id, version_id)
    except VersionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    base = request.app.state.settings.base_path
    return RedirectResponse(
        f"{base}/admin/versions/{version_id}?promoted={channel_id}",
        status_code=302,
    )
