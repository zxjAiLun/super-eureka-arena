"""Worker-side engine-move executor for human-play games.

One short-lived engine process per owed reply: spawn the frozen binary from
the game's ``opponent_snapshot`` (SHA re-verified), UCI handshake, replay the
whole game history via ``position startpos moves ...``, ``go movetime N``,
collect ``bestmove``, quit.  No engine process ever outlives a single move,
so a worker restart, deploy or crash can never leave an orphan engine or a
half-applied game state.

CPU isolation contract (the reason this lives in the worker): the executor is
invoked ONLY from the worker's arbitration loop, interleaved with — never
concurrent with — cutechess pairs and post-game analysis.  A pending human
move is serviced between pair boundaries; while a timed match runs, the
browser simply keeps polling.
"""

from __future__ import annotations

import hashlib
import queue
import subprocess
import threading
import time
from pathlib import Path

import chess

from ..models import HumanGame, HumanGameMove, utcnow


class EngineReplyError(RuntimeError):
    """The engine failed to produce a usable bestmove."""


# Wall-clock deadline per UCI wait. movetime is bounded by settings; this is
# the safety net for a hung engine (handshake, readiness or bestmove).
UCI_WAIT_TIMEOUT = 20.0


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _verify_binary(snapshot: dict) -> Path:
    binary = Path(snapshot["binary_path"])
    if not binary.is_file():
        raise EngineReplyError(f"engine binary missing: {binary}")
    if _sha256_file(binary) != snapshot.get("binary_sha256"):
        raise EngineReplyError("engine binary SHA mismatch")
    return binary


def _line_reader(proc: subprocess.Popen) -> queue.Queue:
    q: queue.Queue = queue.Queue()

    def _run() -> None:
        try:
            for line in proc.stdout:
                q.put(line)
        except Exception as exc:  # noqa: BLE001 - surfaced to the waiter
            q.put(exc)
        finally:
            q.put(None)

    threading.Thread(target=_run, name="human-engine-reader", daemon=True).start()
    return q


def _read_until(reader: queue.Queue, terminator: str,
                timeout: float | None = None) -> list[str]:
    if timeout is None:
        timeout = UCI_WAIT_TIMEOUT
    lines: list[str] = []
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise EngineReplyError(f"engine timed out waiting for {terminator!r}")
        try:
            item = reader.get(timeout=remaining)
        except queue.Empty:
            continue
        if item is None:
            raise EngineReplyError("engine closed output unexpectedly")
        if isinstance(item, Exception):
            raise EngineReplyError(f"engine read failed: {item}")
        lines.append(item)
        if terminator in item:
            return lines


def _send(proc: subprocess.Popen, cmd: str) -> None:
    proc.stdin.write(cmd + "\n")
    proc.stdin.flush()


def _sync(proc: subprocess.Popen, reader: queue.Queue) -> None:
    _send(proc, "isready")
    for _ in _read_until(reader, "readyok"):
        pass


def _terminate(proc: subprocess.Popen) -> None:
    try:
        proc.stdin.close()
    except Exception:
        pass
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
    except Exception:
        pass


def ask_engine_move(snapshot: dict, moves_uci: list[str],
                    movetime_ms: int) -> tuple[str, int]:
    """Run one short-lived engine process and return ``(uci, elapsed_ms)``.

    ``snapshot`` is the frozen opponent launch config; ``moves_uci`` is the
    full UCI move history from the start position.  Raises EngineReplyError
    on any protocol failure — the caller decides how to end the game.
    """
    binary = _verify_binary(snapshot)
    argv = [str(binary)] + [str(a) for a in snapshot.get("command_args") or []]
    started = time.monotonic()
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )
    reader = _line_reader(proc)
    try:
        _send(proc, "uci")
        for _ in _read_until(reader, "uciok"):
            pass
        _sync(proc, reader)
        # Frozen strength options first, then the runtime-reserved options
        # the arena owns (mirroring the cutechess option policy).
        for name, value in sorted((snapshot.get("uci_options") or {}).items()):
            if value is True:
                rendered = "true"
            elif value is False:
                rendered = "false"
            else:
                rendered = str(value)
            _send(proc, f"setoption name {name} value {rendered}")
        _send(proc, "setoption name Threads value 1")
        _send(proc, "setoption name Hash value 64")
        _sync(proc, reader)
        _send(proc, "ucinewgame")
        _sync(proc, reader)
        position = "position startpos"
        if moves_uci:
            position += " moves " + " ".join(moves_uci)
        _send(proc, position)
        _send(proc, f"go movetime {int(movetime_ms)}")
        best_move: str | None = None
        for line in _read_until(reader, "bestmove"):
            tokens = line.strip().split()
            if len(tokens) >= 2 and tokens[0] == "bestmove":
                best_move = tokens[1]
        if not best_move or best_move in ("(none)", "0000"):
            raise EngineReplyError("engine returned no bestmove")
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return best_move, elapsed_ms
    finally:
        _terminate(proc)


def next_pending_game(session) -> HumanGame | None:
    """The oldest ACTIVE game with a pending engine move (FIFO)."""
    return (
        session.query(HumanGame)
        .filter(
            HumanGame.status == "ACTIVE",
            HumanGame.engine_pending.is_(True),
        )
        .order_by(HumanGame.last_move_at.asc())
        .first()
    )


def _moves_uci(session, game: HumanGame) -> list[str]:
    rows = (
        session.query(HumanGameMove)
        .filter(HumanGameMove.human_game_id == game.id)
        .order_by(HumanGameMove.ply.asc())
        .all()
    )
    return [r.uci for r in rows]


def service_pending_move(settings, session, game: HumanGame) -> str:
    """Execute one owed engine move for ``game`` and persist it.

    Returns a short action description for the worker log.  The game may have
    been expired/resigned between the query and this call — everything is
    re-checked under the row's own state, and a stale game is skipped without
    spawning any process.
    """
    game = session.get(HumanGame, game.id)
    if game is None or game.status != "ACTIVE" or not game.engine_pending:
        return "human-move skipped (game no longer pending)"

    snapshot = game.opponent_snapshot or {}
    moves = _moves_uci(session, game)
    board = chess.Board()
    for uci in moves:
        try:
            board.push(chess.Move.from_uci(uci))
        except ValueError as exc:
            # Should be impossible (every stored move was validated on
            # accept); fail the game rather than spawn an engine on a
            # corrupt history.
            game.status = "ENGINE_FAILED"
            game.termination = "engine_error"
            game.result = None
            game.engine_pending = False
            session.commit()
            return f"human-move failed: corrupt history ({exc})"

    if board.is_game_over():
        # Terminal position with a pending flag can only be a race with
        # expiry/finish; clear the flag without inventing a move.
        game.engine_pending = False
        session.commit()
        return "human-move skipped (position already terminal)"

    try:
        best_uci, elapsed_ms = ask_engine_move(
            snapshot, moves, settings.human_play_movetime_ms
        )
    except EngineReplyError as exc:
        game.engine_pending = False
        game.status = "ENGINE_FAILED"
        game.termination = "engine_error"
        game.result = None
        session.commit()
        return f"human-move engine failed: {exc}"

    move = chess.Move.from_uci(best_uci)
    if move not in board.legal_moves:
        # A frozen engine replying outside the rules is treated as an engine
        # failure, never silently substituted.
        game.engine_pending = False
        game.status = "ENGINE_FAILED"
        game.termination = "engine_error"
        game.result = None
        session.commit()
        return "human-move engine failed: illegal bestmove"

    san = board.san(move)
    board.push(move)
    ply = len(moves) + 1
    session.add(
        HumanGameMove(
            human_game_id=game.id,
            ply=ply,
            side="engine",
            uci=best_uci,
            san=san,
            fen_after=board.fen(),
            engine_ms=elapsed_ms,
        )
    )
    game.current_fen = board.fen()
    game.engine_pending = False
    game.revision = (game.revision or 0) + 1
    game.last_move_at = utcnow()
    game.idle_expires_at = utcnow() + _timedelta(
        settings.human_play_idle_seconds
    )

    outcome = board.outcome()
    if outcome is not None:
        game.status = "FINISHED"
        game.result = outcome.result()
        game.termination = (
            outcome.termination.name.lower()
            if outcome.termination is not None
            else "adjudicated"
        )
    session.commit()
    return f"human-move played: game={game.id} ply={ply} uci={best_uci}"


def _timedelta(seconds: int):
    from datetime import timedelta

    return timedelta(seconds=seconds)
