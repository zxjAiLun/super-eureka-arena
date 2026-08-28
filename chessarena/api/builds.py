"""Engine build registry API (section 16.2) + V2.1-B1 admin surface.

v1 does NOT allow uploading binaries through the public API; builds are
installed out-of-band by the deploy pipeline and the ``install_build`` script,
which register the immutable directory in the database.

V2.1-B1 adds the ADMIN surface for the first lifecycle step: turning an
installed build into a candidate EngineVersion — the web equivalent of
``python -m chessarena.admin engine-version create --build ...``.  The form
only accepts version_id / display_name / status(candidate|experimental);
the launch identity is ALWAYS the artifact default (command_args=[],
uci_options={}) and the lifecycle is always hidden/unrated.  There is
deliberately NO generic version editor here.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import EngineBuild, EngineVersion
from ..schemas import BuildOut
from ..security import require_same_origin, validate_csrf_token
from ..services.versions import (
    VersionError,
    create_version_from_build,
    identity_fingerprint,
)

router = APIRouter(tags=["builds"])
admin_router = APIRouter(tags=["admin"], include_in_schema=False)


@router.get("/builds", response_model=list[BuildOut])
def list_builds(session: Session = Depends(get_db)):
    return (
        session.query(EngineBuild)
        .order_by(EngineBuild.created_at.desc())
        .all()
    )


@router.get("/builds/{build_id}", response_model=BuildOut)
def get_build(build_id: str, session: Session = Depends(get_db)):
    build = (
        session.query(EngineBuild)
        .filter(EngineBuild.build_id == build_id)
        .first()
    )
    if build is None:
        raise HTTPException(status_code=404, detail="build not found")
    return build


# ---------------------------------------------------------------------------
# V2.1-B1: admin build inventory + default-identity version creation
# ---------------------------------------------------------------------------
def _default_identity_state(session, build) -> dict:
    """Live default-identity registration state for one build.

    Computes the fingerprint the build's DEFAULT launch identity
    (command_args=[], uci_options={}) would have and looks up the
    EngineVersion that owns it, so the operator sees at a glance whether an
    installed artifact has entered the EngineVersion lifecycle yet.
    """
    fp = identity_fingerprint(build.binary_sha256, [], {})
    version = (
        session.query(EngineVersion)
        .filter(EngineVersion.identity_fingerprint == fp)
        .first()
    )
    return {
        "fingerprint": fp,
        "version": version,  # None => "Not versioned"
    }


@admin_router.get("/admin/builds/", response_class=HTMLResponse)
def admin_builds_list(request: Request, session: Session = Depends(get_db)):
    builds = (
        session.query(EngineBuild)
        .order_by(EngineBuild.created_at.desc())
        .all()
    )
    rows = []
    for b in builds:
        state = _default_identity_state(session, b)
        rows.append({
            "build_id": b.build_id,
            "engine_name": b.engine_name,
            "git_sha": b.git_sha,
            "binary_sha256": b.binary_sha256,
            "enabled": bool(b.enabled),
            "created_at": b.created_at,
            "registered_version": state["version"],
        })
    return request.app.state.templates.TemplateResponse(
        request,
        "admin_builds.html",
        {"builds": rows, "settings": request.app.state.settings},
    )


@admin_router.get(
    "/admin/builds/{build_id}/version/new", response_class=HTMLResponse
)
def admin_build_version_new(
    build_id: str, request: Request, session: Session = Depends(get_db)
):
    build = (
        session.query(EngineBuild)
        .filter(EngineBuild.build_id == build_id)
        .first()
    )
    if build is None:
        raise HTTPException(status_code=404, detail="build not found")
    state = _default_identity_state(session, build)
    # A disabled build can never enter the lifecycle, and a default identity
    # is registered at most once (fingerprint uniqueness).
    blocked_reason = None
    if not build.enabled:
        blocked_reason = "this build is disabled in the registry"
    elif state["version"] is not None:
        blocked_reason = (
            f"default identity already registered by "
            f"{state['version'].version_id}"
        )
    return request.app.state.templates.TemplateResponse(
        request,
        "admin_build_version_new.html",
        {
            "build": build,
            "blocked_reason": blocked_reason,
            "csrf_token": getattr(request.state, "csrf_token", ""),
            "settings": request.app.state.settings,
        },
    )


@admin_router.post(
    "/admin/builds/{build_id}/version",
    response_class=RedirectResponse,
    dependencies=[Depends(require_same_origin)],
)
async def admin_build_version_create(
    build_id: str, request: Request, session: Session = Depends(get_db)
):
    form = dict(await request.form())
    validate_csrf_token(request, form)
    version_id = (form.get("version_id") or "").strip()
    display_name = (form.get("display_name") or "").strip()
    status = (form.get("status") or "candidate").strip()
    if not version_id or not display_name:
        raise HTTPException(status_code=422, detail="version_id and display_name are required")
    if status not in ("candidate", "experimental"):
        raise HTTPException(status_code=422, detail="status must be candidate or experimental")
    try:
        # Web equivalent of `engine-version create --build`: the launch
        # identity is ALWAYS the artifact default and the lifecycle ALWAYS
        # starts hidden/unrated — the form cannot influence either.
        version = create_version_from_build(
            session,
            version_id=version_id,
            display_name=display_name,
            build_id=build_id,
            command_args=[],
            uci_options={},
            status=status,
            rating_enabled=False,
            public_visible=False,
        )
    except VersionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return RedirectResponse(
        f"{request.app.state.settings.base_path}/admin/versions/"
        f"{version.version_id}",
        status_code=302,
    )
