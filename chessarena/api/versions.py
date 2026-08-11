"""EngineVersion / EngineChannel API (S4.3E Phase 1).

EngineVersion is the permanent immutable rated-engine identity
(version == Elo participant). Creation snapshots the launch configuration;
there is NO generic update endpoint for build/launch identity fields.
Channels are the mutable alias (e.g. current-final) pointing at a version.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import EngineChannel, EngineVersion
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
    set_channel,
)

router = APIRouter(tags=["engine-versions"])


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
                status=body.status,
                rating_enabled=body.rating_enabled,
                public_visible=body.public_visible,
            )
        return create_version_from_preset(
            session,
            version_id=body.version_id,
            display_name=body.display_name,
            preset_id=body.preset_id,
            status=body.status,
            rating_enabled=body.rating_enabled,
            public_visible=body.public_visible,
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
)
def set_channel_endpoint(
    channel_id: str,
    body: EngineChannelUpdate,
    session: Session = Depends(get_db),
):
    try:
        return set_channel(session, channel_id, body.engine_version_id)
    except VersionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
