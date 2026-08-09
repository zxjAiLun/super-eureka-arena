"""Tournament engine display labels (P4.6).

The frozen ``config_snapshot`` is the single source of truth for how a side
is shown across admin, public, live and replay surfaces: the snapshot's
``display_name`` (which carries per-match Custom Elo labels) wins, and the
preset/build lookup is only a fallback for legacy snapshots that predate the
frozen label.
"""

from __future__ import annotations

from typing import Any, Optional


def tournament_engine_label(
    session,
    snapshot: Optional[dict],
    preset_id: Optional[str],
    build_id: Optional[str],
    profile: Optional[str],
) -> str:
    snap = snapshot or {}
    frozen = snap.get("display_name")
    if frozen:
        return frozen
    # Legacy fallback: resolve from the live preset/build registry.
    if preset_id:
        from ..models import EnginePreset

        preset = (
            session.query(EnginePreset)
            .filter(EnginePreset.preset_id == preset_id)
            .first()
        )
        if preset is not None:
            return preset.display_name
    name = "ChessEngine"
    if build_id:
        from ..models import EngineBuild

        build = (
            session.query(EngineBuild)
            .filter(EngineBuild.build_id == build_id)
            .first()
        )
        if build is not None:
            name = build.engine_name
    return f"{name} ({profile})" if profile else name
