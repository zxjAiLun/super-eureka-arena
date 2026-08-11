"""Public, read-only replay API (anonymous).

Only result-terminal tournaments (full-schedule COMPLETED or early SPRT
decisions — S4.3D) and their verified games are exposed, with a whitelist of
display fields.  No build ids, binary SHAs, server paths, commands, logs,
manifests or provenance are ever returned.  Write endpoints stay behind the
authenticated ``/api/v1`` tree (enforced by nginx).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..models import (
    ENDED_STATUSES,
    PAUSING,
    QUEUED,
    RESULT_TERMINAL_STATUSES,
    RUNNING,
    EngineBuild,
    EnginePreset,
    Game,
    Tournament,
)
from ..schemas import (
    LiveOut,
    LiveSideOut,
    PublicAnalysisOut,
    PublicGameOut,
    PublicMatchDetailOut,
    PublicMatchOut,
)
from ..services.labels import tournament_engine_label
from ..services.replay import ReplayError, read_single_game_pgn
from ..services.runtime_status import derive_runtime_status

router = APIRouter(tags=["public-replay"])
pages_router = APIRouter(include_in_schema=False, tags=["public-pages"])


def _engine_label(
    session: Session, preset_id: str | None, build_id: str, profile: str
) -> str:
    """Display label for one side (build_id is never exposed).  The frozen
    tournament snapshot's display_name wins; legacy snapshots fall back to
    the preset/build lookup."""
    return tournament_engine_label(
        session, None, preset_id, build_id, profile
    )


def _score_percent(t: Tournament) -> float | None:
    played = t.candidate_wins + t.candidate_losses + t.draws
    if not played:
        return None
    return round((t.candidate_wins + 0.5 * t.draws) / played * 100, 2)


def _public_match(session: Session, t: Tournament) -> PublicMatchOut:
    from ..services.display import match_elo_delta

    snap = t.config_snapshot or {}
    return PublicMatchOut(
        id=t.id,
        name=t.name,
        status=t.status,
        time_control=t.time_control,
        requested_pairs=t.requested_pairs,
        completed_pairs=t.completed_pairs,
        candidate_wins=t.candidate_wins,
        candidate_losses=t.candidate_losses,
        draws=t.draws,
        score_percent=_score_percent(t),
        elo_delta=match_elo_delta(
            t.candidate_wins, t.draws, t.candidate_losses
        ),
        finished_at=t.finished_at,
        engine_a_label=tournament_engine_label(
            session, snap.get("engine_a"),
            t.engine_a_preset_id, t.engine_a_build_id, t.engine_a_profile,
        ),
        engine_b_label=tournament_engine_label(
            session, snap.get("engine_b"),
            t.engine_b_preset_id, t.engine_b_build_id, t.engine_b_profile,
        ),
        opening_set_id=t.opening_set_id,
    )


def _public_game_out(g: Game) -> PublicGameOut:
    from ..services.analysis import result_path

    out = PublicGameOut.model_validate(g)
    out.analyzed = result_path(g.tournament_id, g.id).is_file()
    return out


@router.get("/matches", response_model=list[PublicMatchOut])
def list_public_matches(
    limit: int = 50,
    session: Session = Depends(get_db),
):
    """Public list of result-terminal tournaments (newest first): the full
    schedule COMPLETED or an early SPRT decision (S4.3D)."""
    limit = max(1, min(limit, 200))
    rows = (
        session.query(Tournament)
        .filter(Tournament.status.in_(RESULT_TERMINAL_STATUSES))
        .order_by(Tournament.finished_at.desc())
        .limit(limit)
        .all()
    )
    return [_public_match(session, t) for t in rows]


@router.get("/matches/{tournament_id}", response_model=PublicMatchDetailOut)
def get_public_match(
    tournament_id: str,
    session: Session = Depends(get_db),
):
    """Public detail of a result-terminal tournament plus its verified games."""
    t = (
        session.query(Tournament)
        .filter(Tournament.id == tournament_id)
        .first()
    )
    if t is None or t.status not in RESULT_TERMINAL_STATUSES:
        raise HTTPException(status_code=404, detail="match not found")
    games = (
        session.query(Game)
        .filter(Game.tournament_id == t.id, Game.verified.is_(True))
        .order_by(Game.game_number)
        .all()
    )
    match = _public_match(session, t)
    return PublicMatchDetailOut(
        **match.model_dump(),
        games=[_public_game_out(g) for g in games],
    )


@router.get("/games/{game_id}/pgn")
def get_public_game_pgn(
    game_id: str,
    session: Session = Depends(get_db),
):
    """Single verified game's PGN text (anonymous replay)."""
    game = session.query(Game).filter(Game.id == game_id).first()
    if game is None or not game.verified:
        raise HTTPException(status_code=404, detail="game not found")
    tournament = (
        session.query(Tournament)
        .filter(Tournament.id == game.tournament_id)
        .first()
    )
    if tournament is None or tournament.status not in RESULT_TERMINAL_STATUSES:
        raise HTTPException(status_code=404, detail="game not found")
    try:
        pgn = read_single_game_pgn(game)
    except ReplayError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    filename = f"game-{game.game_number}.pgn"
    return PlainTextResponse(
        pgn,
        media_type="application/x-chess-pgn",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/games/{game_id}/analysis", response_model=PublicAnalysisOut)
def get_public_game_analysis(
    game_id: str,
    session: Session = Depends(get_db),
    settings=Depends(get_settings),
):
    """Whitelisted per-game Stockfish analysis (P4.7).  404 when the game is
    not analyzed (the replay degrades to a normal replay)."""
    from ..services.analysis import AnalysisError, read_analysis

    game = session.query(Game).filter(Game.id == game_id).first()
    if game is None or not game.verified:
        raise HTTPException(status_code=404, detail="game not found")
    tournament = (
        session.query(Tournament)
        .filter(Tournament.id == game.tournament_id)
        .first()
    )
    if tournament is None or tournament.status not in RESULT_TERMINAL_STATUSES:
        raise HTTPException(status_code=404, detail="game not found")
    try:
        data = read_analysis(game)
    except AnalysisError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if data is None:
        raise HTTPException(status_code=404, detail="game not analyzed")
    return PublicAnalysisOut(
        engine_name=data["engine"]["name"],
        limit=data["limit"],
        positions=data["positions"],
    )


# ---------------------------------------------------------------------------
# Live match status (P4.3 v1) — unverified runtime data, read-only.
# ---------------------------------------------------------------------------
def _current_pair(t: Tournament):
    """The pair that is executing right now (status RUNNING), else the most
    recently started pair that already has a run directory."""
    running = [
        p for p in t.pair_jobs if p.status == RUNNING and p.run_directory
    ]
    if running:
        return max(running, key=lambda p: p.started_at or datetime.min)
    started = [p for p in t.pair_jobs if p.run_directory and p.started_at]
    if started:
        return max(started, key=lambda p: p.started_at or datetime.min)
    return None


def _live_payload(session: Session, settings, t: Tournament) -> dict:
    """Build the LiveOut payload for a single tournament."""
    base = {
        "tournament_id": t.id,
        "name": t.name,
        "engine_a_label": tournament_engine_label(
            session, (t.config_snapshot or {}).get("engine_a"),
            t.engine_a_preset_id, t.engine_a_build_id, t.engine_a_profile,
        ),
        "engine_b_label": tournament_engine_label(
            session, (t.config_snapshot or {}).get("engine_b"),
            t.engine_b_preset_id, t.engine_b_build_id, t.engine_b_profile,
        ),
        "time_control": t.time_control,
        "opening_set_id": t.opening_set_id,
        "pairs_total": t.requested_pairs,
        "candidate_wins": t.candidate_wins,
        "candidate_losses": t.candidate_losses,
        "draws": t.draws,
    }
    if t.status in ENDED_STATUSES:
        result = {
            **base,
            "status": "completed",
        }
        # Only result-bearing terminals (COMPLETED / SPRT decisions) get a
        # replay link: FAILED/CANCELLED matches have nothing to replay, and
        # their /matches/{id} would 404.
        if t.status in RESULT_TERMINAL_STATUSES:
            result["match_url"] = f"{settings.base_path}/matches/{t.id}"
        return result

    pair = _current_pair(t)
    if pair and pair.run_directory and Path(pair.run_directory).is_dir():
        run_dir = Path(pair.run_directory)
        opening_fen = None
        op = run_dir / "opening.epd"
        if op.is_file():
            opening_fen = op.read_text(encoding="utf-8").strip() or None

        from ..services.live_telemetry import parse_live_state

        # P4.11 repair: with a -debug stream the match-facing boundary lines
        # (Started/Finished game) are inside the bounded tail read, so /live
        # never scans the whole (potentially 50MB+) stdout.log.
        telemetry = None
        stdout = run_dir / "stdout.log"
        if stdout.is_file():
            try:
                telemetry = parse_live_state(stdout)
            except Exception:
                telemetry = None
        if telemetry and telemetry.get("has_debug"):
            runtime = {
                "game_in_pair": telemetry.get("game_in_pair"),
                "total_games": 2,
                "state": telemetry.get("state"),
                "last_result": telemetry.get("last_result"),
            }
        else:
            # Pre-P4.11 match: no -debug stream, log is small enough to scan.
            runtime = derive_runtime_status(run_dir, total_games=2)

        payload = {
            **base,
            "status": "live",
            "pair_index": pair.pair_index,
            "game_in_pair": runtime.get("game_in_pair"),
            "games_total": runtime.get("total_games"),
            "state": runtime.get("state"),
            "last_result": runtime.get("last_result"),
            "opening_fen": opening_fen,
        }
        _attach_live_telemetry(payload, run_dir, opening_fen, telemetry)
        return payload
    return {**base, "status": "live"}


def _attach_live_telemetry(payload: dict, run_dir: Path, opening_fen,
                           telemetry: Optional[dict] = None) -> None:
    """P4.11: parse the cutechess -debug stream and fill the live fields.

    The current position is the real one from the engine protocol stream
    (falls back to the pair's opening FEN when the stream is unavailable);
    colors follow the pair contract: debug game 0 = engine A white.
    """
    import time as _time

    from ..services.live_telemetry import parse_live_state

    stdout = run_dir / "stdout.log"
    if not stdout.is_file():
        return
    if telemetry is None:
        try:
            telemetry = parse_live_state(stdout)
        except Exception:
            return
    payload["current_fen"] = telemetry.get("current_fen") or opening_fen
    payload["side_to_move"] = telemetry.get("side_to_move")
    payload["last_move"] = telemetry.get("last_move")
    payload["ply"] = telemetry.get("ply")
    try:
        payload["telemetry_age_s"] = int(
            _time.time() - stdout.stat().st_mtime
        )
    except OSError:
        pass

    engines = telemetry.get("engines") or {}
    go = telemetry.get("go") or {}
    if not go:
        return  # no engine search stream yet (e.g. pre-P4.11 matches)
    a_label = payload["engine_a_label"]
    b_label = payload["engine_b_label"]
    active = telemetry.get("active_engine")

    def _clock_ms(color: str) -> Optional[int]:
        """White/Black absolute clock from the latest go, minus the active
        engine's current search time (info time)."""
        base = go.get("wtime") if color == "w" else go.get("btime")
        if base is None:
            return None
        if active is not None:
            eng = engines.get(active) or {}
            time_ms = eng.get("time_ms")
            # The active engine is the side to move: its clock only runs down
            # for the color that is to move.
            if payload.get("side_to_move") == color and time_ms is not None:
                return max(0, base - time_ms)
        return base

    def _side(index: int, label: str, color: str) -> LiveSideOut:
        eng = engines.get(index) or {}
        return LiveSideOut(
            label=label,
            clock_ms=_clock_ms(color),
            eval_cp=eng.get("eval_cp"),
            mate=eng.get("mate"),
            depth=eng.get("depth"),
            nodes=eng.get("nodes"),
            nps=eng.get("nps"),
            pv=eng.get("pv") or [],
        )

    # Colors follow the pair contract: game 1 (odd game_in_pair) -> engine A
    # white, game 2 -> engine A black.  Engine index 0 is always engine A.
    # Without an authoritative game boundary (Started game line) we fail
    # closed and show no sides rather than guess the colors.
    game_in_pair = payload.get("game_in_pair")
    if game_in_pair is None:
        return
    a_white = game_in_pair % 2 == 1
    payload["white"] = _side(0 if a_white else 1, a_label if a_white else b_label, "w")
    payload["black"] = _side(1 if a_white else 0, b_label if a_white else a_label, "b")


@router.get("/live", response_model=LiveOut)
def get_live_status(
    tournament_id: Optional[str] = None,
    session: Session = Depends(get_db),
    settings=Depends(get_settings),
):
    """Current live match status for the watched (or auto-detected) match.

    Pass ``tournament_id`` to pin a specific match; otherwise the most recently
    started queued/running match is reported.  Returns ``idle`` when there is
    nothing running.
    """
    if tournament_id:
        t = (
            session.query(Tournament)
            .filter(Tournament.id == tournament_id)
            .first()
        )
        if t is None:
            return LiveOut(status="idle")
        return LiveOut(**_live_payload(session, settings, t))
    t = (
        session.query(Tournament)
        # PAUSED sits on a pair boundary with no live game; only queued,
        # running and pausing matches are live-watchable.
        .filter(
            Tournament.status.in_([QUEUED, RUNNING, PAUSING])
        )
        .order_by(Tournament.started_at.desc())
        .first()
    )
    if t is None:
        return LiveOut(status="idle")
    return LiveOut(**_live_payload(session, settings, t))


# ---------------------------------------------------------------------------
# Public HTML pages
# ---------------------------------------------------------------------------
def _render(request: Request, template: str, **ctx):
    return request.app.state.templates.TemplateResponse(
        request, template, ctx
    )


def _recent_matches(session: Session, limit: int = 12):
    rows = (
        session.query(Tournament)
        .filter(Tournament.status.in_(RESULT_TERMINAL_STATUSES))
        .order_by(Tournament.finished_at.desc())
        .limit(limit)
        .all()
    )
    return [_public_match(session, t) for t in rows]


@pages_router.get("/")
def public_home(request: Request, session: Session = Depends(get_db)):
    """Arena Overview (P4.11 commit 4): what is happening now, the latest
    completed result, and a compact recent list — NOT a copy of the full
    matches table (that lives on /matches/)."""
    live = (
        session.query(Tournament)
        .filter(Tournament.status.in_([QUEUED, RUNNING, PAUSING]))
        .order_by(Tournament.started_at.desc())
        .first()
    )
    live_pair = _current_pair(live) if live is not None else None
    latest = (
        session.query(Tournament)
        .filter(Tournament.status.in_(RESULT_TERMINAL_STATUSES))
        .order_by(Tournament.finished_at.desc())
        .first()
    )
    latest_match = _public_match(session, latest) if latest is not None else None
    recent = _recent_matches(session, limit=5)
    return _render(
        request,
        "public_home.html",
        settings=request.app.state.settings,
        live=live,
        live_pair_index=live_pair.pair_index if live_pair else None,
        latest=latest_match,
        matches=recent,
    )


@pages_router.get("/live")
def public_live(
    request: Request,
    tournament_id: Optional[str] = None,
    session: Session = Depends(get_db),
):
    """Live match status page (P4.3 v1).  The React bundle runs in ``live``
    mode, polling the public /live JSON endpoint every couple of seconds."""
    pinned = tournament_id
    if pinned is not None:
        t = (
            session.query(Tournament)
            .filter(Tournament.id == pinned)
            .first()
        )
        if t is None:
            raise HTTPException(status_code=404, detail="match not found")
    return _render(
        request,
        "public_live.html",
        settings=request.app.state.settings,
        tournament_id=pinned,
    )


@pages_router.get("/matches/")
def public_matches(request: Request, session: Session = Depends(get_db)):
    return _render(
        request,
        "public_matches.html",
        settings=request.app.state.settings,
        matches=_recent_matches(session, limit=200),
    )


@pages_router.get("/ratings/")
def public_ratings(
    request: Request,
    tc: str = "blitz_3_2",
    session: Session = Depends(get_db),
):
    """Public Arena Elo leaderboard (P4.11 commit 4), recomputed live from
    rated match history via the same compute_ratings service the admin used.
    Only whitelisted display fields reach the template — never fingerprints,
    build ids, SHAs or server paths."""
    from ..config import TIME_CONTROLS
    from ..models import EngineChannel, EngineVersion
    from ..services.display import tc_label
    from ..services.ratings import compute_ratings

    all_ratings = compute_ratings(session)
    pools = {key: tc_label(key) for key in TIME_CONTROLS}
    selected = tc if tc in pools else "blitz_3_2"
    rows = all_ratings.get(selected, {"engines": [], "anchors": []})
    # S4.3E Phase 1: channel mapping for the EngineVersion identity columns.
    channels = session.query(EngineChannel).order_by(
        EngineChannel.channel_id.asc()
    ).all()
    channel_map = {c.engine_version_id: c.channel_id for c in channels}
    engines = []
    for e in rows["engines"]:
        pid = e.get("participant_id") or ""
        if e["status"] == "fixed":
            version_id = None
            identity = None
        elif pid.startswith("legacy:"):
            version_id = None
            identity = pid
        else:
            version_id = pid
            identity = None
        engines.append(
            {
                "display_name": e["display_name"],
                "rating": e["rating"],
                "games": e["games"],
                "wins": e["wins"],
                "draws": e["draws"],
                "losses": e["losses"],
                "status": e["status"],
                "version_id": version_id,
                "identity": identity,
                "channel": channel_map.get(version_id),
            }
        )
    # S4.3E Phase 1: version catalog + channel mapping on the public page.
    versions = session.query(EngineVersion).order_by(
        EngineVersion.created_at.asc()
    ).all()
    version_rows = [
        {
            "version_id": v.version_id,
            "display_name": v.display_name,
            "status": v.status,
            "build_id": v.build_id,
            "channel": channel_map.get(v.version_id),
        }
        for v in versions
        if v.public_visible and v.rating_enabled
    ]
    return _render(
        request,
        "public_ratings.html",
        settings=request.app.state.settings,
        pools=pools,
        selected_tc=selected,
        engines=engines,
        anchors=rows["anchors"],
        versions=version_rows,
    )


@pages_router.get("/matches/{tournament_id}")
def public_match_detail(
    tournament_id: str,
    request: Request,
    session: Session = Depends(get_db),
):
    t = (
        session.query(Tournament)
        .filter(Tournament.id == tournament_id)
        .first()
    )
    if t is None or t.status not in RESULT_TERMINAL_STATUSES:
        raise HTTPException(status_code=404, detail="match not found")
    games = (
        session.query(Game)
        .filter(Game.tournament_id == t.id, Game.verified.is_(True))
        .order_by(Game.game_number)
        .all()
    )
    match = _public_match(session, t)
    detail = PublicMatchDetailOut(
        **match.model_dump(),
        games=[_public_game_out(g) for g in games],
    )
    return _render(
        request,
        "public_match_detail.html",
        settings=request.app.state.settings,
        match=detail,
    )


@pages_router.get("/games/{game_id}")
def public_game(
    game_id: str,
    request: Request,
    session: Session = Depends(get_db),
):
    """Official game replay page (React + react-chessboard + chess.js).

    The template only mounts the replay root; the React app fetches the public
    match-detail and PGN APIs.
    """
    game = session.query(Game).filter(Game.id == game_id).first()
    if game is None or not game.verified:
        raise HTTPException(status_code=404, detail="game not found")
    tournament = (
        session.query(Tournament)
        .filter(Tournament.id == game.tournament_id)
        .first()
    )
    if tournament is None or tournament.status not in RESULT_TERMINAL_STATUSES:
        raise HTTPException(status_code=404, detail="game not found")
    pair_index = game.pair_job.pair_index if game.pair_job else 0
    return _render(
        request,
        "public_game.html",
        settings=request.app.state.settings,
        game=game,
        tournament_id=game.tournament_id,
        pair_index=pair_index,
    )
