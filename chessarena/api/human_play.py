"""Human vs Engine play API (dark launch, anonymous, token-authorized).

Every route — JSON and HTML — fails closed with 404 while
``ARENA_HUMAN_PLAY_ENABLED`` is off, so the feature's public surface stays
invisible until the operator flips the flag.

Authorization model:
- game creation is anonymous but IP rate-limited;
- every subsequent request must carry the per-game secret
  (``X-Game-Token`` header) handed out exactly once at creation;
- state-changing requests additionally require same-origin and the
  ``X-CSRF-Token`` header (double-submit cookie), never one without the
  other.

Responses leak no provenance: opponent snapshots, build ids, binary SHAs,
paths and creator IPs never leave the server.

Settings are taken from ``request.app.state.settings`` (the exact Settings
the app was built with) rather than the ``get_settings`` dependency so a
test-injected app instance is fully honored.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from ..config import Settings
from ..db import get_db
from ..models import HUMAN_GAME_TERMINAL_STATUSES
from ..schemas import (
    HumanGameCreate,
    HumanGameCreateOut,
    HumanGameOut,
    HumanMoveIn,
    HumanOpponentOut,
)
from ..security import require_same_origin, validate_csrf_header
from ..services import human_game
from ..services.human_play import list_opponents

router = APIRouter(tags=["human-play"])
pages_router = APIRouter(include_in_schema=False, tags=["human-play-pages"])


def _game_token(request: Request) -> str | None:
    return request.headers.get("X-Game-Token")


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _require_enabled(settings: Settings) -> None:
    if not settings.human_play_enabled:
        raise HTTPException(status_code=404, detail="not found")


def _load_game(request: Request, session: Session, game_id: str):
    settings = _settings(request)
    _require_enabled(settings)
    try:
        return human_game.get_game(
            session, settings, game_id, _game_token(request)
        )
    except human_game.HumanPlayError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))


# ---------------------------------------------------------------------------
# JSON API
# ---------------------------------------------------------------------------
@router.get("/human-play/opponents", response_model=list[HumanOpponentOut])
def list_human_opponents(request: Request, session: Session = Depends(get_db)):
    """Selectable opponents (explicit allowlist order, whitelisted fields)."""
    settings = _settings(request)
    _require_enabled(settings)
    return list_opponents(session, settings.human_play_opponent_refs())


@router.post(
    "/human-play/games", response_model=HumanGameCreateOut, status_code=201
)
def create_human_game(
    payload: HumanGameCreate,
    request: Request,
    session: Session = Depends(get_db),
):
    """Create a game and return its secret token once (``game_token``)."""
    settings = _settings(request)
    _require_enabled(settings)
    require_same_origin(request)
    validate_csrf_header(request)
    try:
        game, token = human_game.create_game(
            session, settings, request, payload.opponent,
            payload.human_color,
        )
    except human_game.HumanPlayError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))

    body = human_game.game_payload(game)
    body["game_token"] = token
    return body


@router.get("/human-play/games/{game_id}", response_model=HumanGameOut)
def get_human_game(
    game_id: str,
    request: Request,
    session: Session = Depends(get_db),
):
    """Authoritative game state; the browser polls this while a move is
    pending."""
    game = _load_game(request, session, game_id)
    if game.status in HUMAN_GAME_TERMINAL_STATUSES:
        human_game.ensure_pgn(session, _settings(request), game)
    return human_game.game_payload(game)


@router.post("/human-play/games/{game_id}/moves", response_model=HumanGameOut)
def submit_human_move(
    game_id: str,
    payload: HumanMoveIn,
    request: Request,
    session: Session = Depends(get_db),
):
    """Submit one human move; the engine's reply arrives via polling (the
    worker owes it, 202-style)."""
    settings = _settings(request)
    game = _load_game(request, session, game_id)
    require_same_origin(request)
    validate_csrf_header(request)
    try:
        game = human_game.submit_human_move(
            session, settings, game, payload.uci, payload.expected_revision
        )
    except human_game.HumanPlayError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))
    if game.status in HUMAN_GAME_TERMINAL_STATUSES:
        human_game.ensure_pgn(session, settings, game)
    return human_game.game_payload(game)


@router.post("/human-play/games/{game_id}/resign", response_model=HumanGameOut)
def resign_human_game(
    game_id: str,
    request: Request,
    session: Session = Depends(get_db),
):
    settings = _settings(request)
    game = _load_game(request, session, game_id)
    require_same_origin(request)
    validate_csrf_header(request)
    try:
        game = human_game.resign_game(session, settings, game)
    except human_game.HumanPlayError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))
    return human_game.game_payload(game)


@router.get("/human-play/games/{game_id}/pgn")
def get_human_game_pgn(
    game_id: str,
    request: Request,
    session: Session = Depends(get_db),
):
    """Download the PGN of a terminal game."""
    game = _load_game(request, session, game_id)
    if game.status not in HUMAN_GAME_TERMINAL_STATUSES:
        raise HTTPException(status_code=409, detail="game is not finished")
    text = human_game.ensure_pgn(session, _settings(request), game)
    return PlainTextResponse(
        text,
        media_type="application/x-chess-pgn",
        headers={
            "Content-Disposition": (
                f'attachment; filename="human-game-{game.id}.pgn"'
            )
        },
    )


# ---------------------------------------------------------------------------
# HTML page
# ---------------------------------------------------------------------------
@pages_router.get("/human-play/")
def human_play_page(request: Request):
    """The play page.  404 while the feature flag is off."""
    settings = _settings(request)
    _require_enabled(settings)
    csrf = getattr(request.state, "csrf_token", "")
    return request.app.state.templates.TemplateResponse(
        request,
        "public_human_play.html",
        {
            "settings": settings,
            "csrf_token": csrf,
            "poll_seconds": settings.human_play_poll_seconds,
        },
    )
