"""Human-play game state machine (API side).

Owns everything except the engine execution itself (worker-side
``services.human_engine``): game creation with rate limits and frozen
opponent snapshots, lazy TTL/idle expiry, authoritative move validation with
revision-based optimistic concurrency, resign, and PGN synthesis for
terminal games.

All state transitions are guarded by the per-game secret token (hashed at
rest, constant-time compare) plus CSRF/same-origin checks applied at the
router layer.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta
from pathlib import Path
from typing import Optional

import chess
import chess.pgn
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import (
    HUMAN_GAME_ACTIVE,
    HUMAN_GAME_ENGINE_FAILED,
    HUMAN_GAME_EXPIRED,
    HUMAN_GAME_FINISHED,
    HUMAN_GAME_INTERRUPTED,
    HUMAN_GAME_RESIGNED,
    HUMAN_GAME_TERMINAL_STATUSES,
    HumanGame,
    HumanGameMove,
    coerce_utc,
    utcnow,
)
from .artifacts import configure_artifact_service
from .human_play import OpponentError, resolve_opponent


class HumanPlayError(Exception):
    """Domain error; the router maps it to a specific status code."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_game_token() -> str:
    return secrets.token_hex(32)


# ---------------------------------------------------------------------------
# Rate limits
# ---------------------------------------------------------------------------
def _client_ip(request) -> str:
    # nginx passes X-Forwarded-For; trust the LAST entry (appended by our own
    # proxy) as the client address, falling back to the socket peer.
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[-1].strip()[:64]
    if request.client and request.client.host:
        return request.client.host[:64]
    return "unknown"


def _enforce_limits(session: Session, settings: Settings, ip: str) -> None:
    now = utcnow()
    active_total = (
        session.query(HumanGame)
        .filter(HumanGame.status == HUMAN_GAME_ACTIVE)
        .count()
    )
    if active_total >= settings.human_play_max_total_active:
        raise HumanPlayError("server is busy, try again later", 503)
    active_ip = (
        session.query(HumanGame)
        .filter(
            HumanGame.creator_ip == ip,
            HumanGame.status == HUMAN_GAME_ACTIVE,
        )
        .count()
    )
    if active_ip >= settings.human_play_max_active_per_ip:
        raise HumanPlayError(
            "too many active games for this address", 429
        )
    created_hour_ago = (
        session.query(HumanGame)
        .filter(
            HumanGame.creator_ip == ip,
            HumanGame.created_at >= now - timedelta(hours=1),
        )
        .count()
    )
    if created_hour_ago >= settings.human_play_max_created_per_hour:
        raise HumanPlayError("creation rate limit exceeded", 429)


# ---------------------------------------------------------------------------
# Expiry (lazy)
# ---------------------------------------------------------------------------
def apply_lazy_expiry(session: Session, game: HumanGame) -> HumanGame:
    """Expire an ACTIVE game past its TTL or idle deadline; commit happens
    in the caller's transaction."""
    if game.status != HUMAN_GAME_ACTIVE:
        return game
    now = utcnow()
    expired = None
    expires_at = coerce_utc(game.expires_at)
    idle_expires_at = coerce_utc(game.idle_expires_at)
    if expires_at is not None and now >= expires_at:
        expired = "ttl_expired"
    elif idle_expires_at is not None and now >= idle_expires_at:
        expired = "idle_expired"
    if expired is None:
        return game
    game.status = HUMAN_GAME_EXPIRED
    game.termination = expired
    game.engine_pending = False
    # A game that expired while the engine owed a move has no fair result.
    game.result = None
    session.add(game)
    return game


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------
START_FEN = chess.STARTING_FEN


def create_game(
    session: Session, settings: Settings, request, opponent_ref: str,
    human_color: str,
) -> tuple[HumanGame, str]:
    """Create a new game. Returns ``(game, plain_token)``; the token is handed
    to the browser exactly once and only its hash is stored."""
    if not settings.human_play_enabled:
        raise HumanPlayError("human play is disabled", 404)
    ip = _client_ip(request)
    _enforce_limits(session, settings, ip)

    try:
        choice = resolve_opponent(
            session, opponent_ref, settings.human_play_opponent_refs()
        )
    except OpponentError as exc:
        raise HumanPlayError(str(exc), 404) from exc

    token = new_game_token()
    now = utcnow()
    board = chess.Board()
    game = HumanGame(
        game_token_hash=hash_token(token),
        opponent_kind=choice.kind,
        opponent_ref=choice.ref,
        opponent_snapshot=choice.to_snapshot(),
        human_color=human_color,
        status=HUMAN_GAME_ACTIVE,
        current_fen=board.fen(),
        revision=0,
        engine_pending=False,
        creator_ip=ip,
        created_at=now,
        last_move_at=now,
        expires_at=now + timedelta(seconds=settings.human_play_ttl_seconds),
        idle_expires_at=now
        + timedelta(seconds=settings.human_play_idle_seconds),
    )
    session.add(game)
    session.commit()
    return game, token


# ---------------------------------------------------------------------------
# Auth + fetch
# ---------------------------------------------------------------------------
def authorize(game: HumanGame, token: str | None) -> HumanGame:
    if not token:
        raise HumanPlayError("missing game token", 401)
    if not secrets.compare_digest(hash_token(token), game.game_token_hash):
        raise HumanPlayError("invalid game token", 401)
    return game


def get_game(session: Session, settings: Settings, game_id: str,
             token: str | None) -> HumanGame:
    game = session.query(HumanGame).filter(HumanGame.id == game_id).first()
    if game is None:
        # Indistinguishable from a token failure: never confirm game ids.
        raise HumanPlayError("game not found", 401)
    authorize(game, token)
    apply_lazy_expiry(session, game)
    session.commit()
    return game


# ---------------------------------------------------------------------------
# Moves
# ---------------------------------------------------------------------------
def submit_human_move(
    session: Session, settings: Settings, game: HumanGame, uci: str,
    expected_revision: int,
) -> HumanGame:
    """Validate and record one human move, then mark the engine reply as
    pending. Raises on any mismatch; caller returns 409/400/410."""
    if not settings.human_play_enabled:
        raise HumanPlayError("human play is disabled", 404)
    apply_lazy_expiry(session, game)
    if game.status != HUMAN_GAME_ACTIVE:
        raise HumanPlayError("game is not active", 410)
    if game.engine_pending:
        raise HumanPlayError("engine move still pending", 409)
    if (game.revision or 0) != expected_revision:
        raise HumanPlayError("stale revision", 409)

    board = chess.Board(game.current_fen)
    human_turn = (
        board.turn == chess.WHITE
        if game.human_color == "white"
        else board.turn == chess.BLACK
    )
    if not human_turn:
        raise HumanPlayError("not your turn", 409)

    try:
        move = chess.Move.from_uci(uci)
    except ValueError as exc:
        raise HumanPlayError(f"malformed move: {uci}") from exc
    if move not in board.legal_moves:
        raise HumanPlayError(f"illegal move: {uci}")

    san = board.san(move)
    board.push(move)
    ply = (
        session.query(HumanGameMove)
        .filter(HumanGameMove.human_game_id == game.id)
        .count()
    ) + 1
    session.add(
        HumanGameMove(
            human_game_id=game.id,
            ply=ply,
            side="human",
            uci=uci,
            san=san,
            fen_after=board.fen(),
        )
    )
    game.current_fen = board.fen()
    game.revision = (game.revision or 0) + 1
    game.last_move_at = utcnow()
    game.idle_expires_at = utcnow() + timedelta(
        seconds=settings.human_play_idle_seconds
    )

    outcome = board.outcome()
    if outcome is not None:
        game.status = HUMAN_GAME_FINISHED
        game.result = outcome.result()
        game.termination = (
            outcome.termination.name.lower()
            if outcome.termination is not None
            else "adjudicated"
        )
        game.engine_pending = False
    else:
        # Engine owes the reply; the worker services it between pairs.
        game.engine_pending = True
    session.commit()
    return game


# ---------------------------------------------------------------------------
# Resign
# ---------------------------------------------------------------------------
def resign_game(
    session: Session, settings: Settings, game: HumanGame
) -> HumanGame:
    apply_lazy_expiry(session, game)
    if game.status != HUMAN_GAME_ACTIVE:
        raise HumanPlayError("game is not active", 410)
    game.status = HUMAN_GAME_RESIGNED
    game.termination = "resign"
    game.engine_pending = False
    # Standard resignation result: the resigning side loses.
    game.result = "0-1" if game.human_color == "white" else "1-0"
    game.last_move_at = utcnow()
    session.commit()
    _write_pgn(session, settings, game)
    return game


# ---------------------------------------------------------------------------
# PGN
# ---------------------------------------------------------------------------
def human_games_dir(settings: Settings) -> Path:
    return settings.run_root / "human-games"


def game_pgn_path(settings: Settings, game: HumanGame) -> Path:
    return human_games_dir(settings) / f"{game.id}.pgn"


def build_pgn(game: HumanGame, moves: list[HumanGameMove]) -> str:
    """Synthesize the game PGN from the move log."""
    board = chess.Board()
    pgn_game = chess.pgn.Game()
    human = "Human"
    engine = (game.opponent_snapshot or {}).get("display_name", "Engine")
    if game.human_color == "white":
        pgn_game.headers["White"] = human
        pgn_game.headers["Black"] = engine
    else:
        pgn_game.headers["White"] = engine
        pgn_game.headers["Black"] = human
    pgn_game.headers["Event"] = "ChessArena Human vs Engine"
    pgn_game.headers["Site"] = "ChessArena"
    pgn_game.headers["Date"] = game.created_at.strftime("%Y.%m.%d")
    if game.result:
        pgn_game.headers["Result"] = game.result
    if game.termination:
        pgn_game.headers["Termination"] = game.termination
    node = pgn_game
    for m in moves:
        node = node.add_main_variation(chess.Move.from_uci(m.uci))
    return str(pgn_game)


def _write_pgn(session: Session, settings: Settings, game: HumanGame) -> None:
    """Write the PGN artifact for a terminal game (best-effort)."""
    if game.status not in HUMAN_GAME_TERMINAL_STATUSES:
        return
    moves = (
        session.query(HumanGameMove)
        .filter(HumanGameMove.human_game_id == game.id)
        .order_by(HumanGameMove.ply.asc())
        .all()
    )
    text = build_pgn(game, moves)
    path = game_pgn_path(settings, game)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".pgn.tmp")
    tmp.write_text(text + "\n", encoding="utf-8")
    tmp.replace(path)
    game.pgn_path = str(path)


def ensure_pgn(session: Session, settings: Settings, game: HumanGame) -> str:
    """Return the game's PGN text, generating the artifact when missing."""
    path = game_pgn_path(settings, game)
    if not path.is_file():
        _write_pgn(session, settings, game)
        session.commit()
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------
def game_payload(game: HumanGame) -> dict:
    """Whitelisted state payload for the browser."""
    board = chess.Board(game.current_fen)
    side_to_move = "white" if board.turn == chess.WHITE else "black"
    return {
        "id": game.id,
        "status": game.status,
        "human_color": game.human_color,
        "opponent_name": (game.opponent_snapshot or {}).get(
            "display_name", "Engine"
        ),
        "opponent_kind": (game.opponent_snapshot or {}).get("kind", "engine"),
        "revision": game.revision or 0,
        "engine_pending": bool(game.engine_pending),
        "fen": game.current_fen,
        "side_to_move": side_to_move,
        "result": game.result,
        "termination": game.termination,
        "in_check": board.is_check(),
        "moves": [
            {
                "ply": m.ply,
                "side": m.side,
                "uci": m.uci,
                "san": m.san,
                "fen_after": m.fen_after,
                "engine_ms": m.engine_ms,
            }
            for m in game.moves
        ],
    }
