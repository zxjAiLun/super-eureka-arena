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
from sqlalchemy import case, or_, update

from ..models import HumanGame, HumanGameMove, utcnow
from .human_game import game_is_stale


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
    """The oldest ACTIVE game with a pending engine move (FIFO), skipping
    rows already past TTL/idle — an expired game must never spawn an engine
    even before its owning browser triggers lazy expiry.

    Stale rows encountered while scanning are expired IN PLACE, but this
    function never commits: the CALLER must commit (or roll back with the
    rest of its transaction) so the expiry actually persists.  A plain
    Session close silently rolls back, which would re-scan the same dead
    row on every worker tick."""
    rows = (
        session.query(HumanGame)
        .filter(
            HumanGame.status == "ACTIVE",
            HumanGame.engine_pending.is_(True),
        )
        .order_by(HumanGame.last_move_at.asc())
        .all()
    )
    for row in rows:
        if game_is_stale(row):
            # Lazily expire in place so it stops being returned.
            row.status = "EXPIRED"
            row.termination = (
                "ttl_expired"
                if _past(row.expires_at)
                else "idle_expired"
            )
            row.engine_pending = False
            row.result = None
            session.add(row)
        else:
            return row
    return None


def _past(dt) -> bool:
    from ..models import coerce_utc

    dt = coerce_utc(dt)
    return dt is not None and utcnow() >= dt


def _moves_uci(session, game: HumanGame) -> list[str]:
    rows = (
        session.query(HumanGameMove)
        .filter(HumanGameMove.human_game_id == game.id)
        .order_by(HumanGameMove.ply.asc())
        .all()
    )
    return [r.uci for r in rows]


def _fail_game(session, game_id: str, expected_revision: int,
               reason: str) -> bool:
    """Mark a game ENGINE_FAILED via the SAME ownership CAS as the success
    path (status=ACTIVE, engine_pending=true, revision=expected).

    Without the pending+revision conditions a late failure from a worker
    that lost the race would kill a healthy game that another writer
    already advanced (or the human already replied to).  Returns True only
    when this call still owned the row (rowcount == 1).
    """
    stmt = (
        update(HumanGame)
        .where(
            HumanGame.id == game_id,
            HumanGame.status == "ACTIVE",
            HumanGame.engine_pending.is_(True),
            HumanGame.revision == expected_revision,
        )
        .values(
            status="ENGINE_FAILED",
            termination="engine_error",
            result=None,
            engine_pending=False,
        )
    )
    result = session.execute(
        stmt, execution_options={"synchronize_session": False}
    )
    session.commit()
    session.expire_all()
    return result.rowcount == 1


def _expire_if_stale_owner(session, game_id: str,
                           expected_revision: int) -> bool:
    """Terminalize a row whose deadline passed mid-search.

    Conditional on the SAME ownership token (ACTIVE + pending +
    expected_revision) plus ``deadline <= now`` — so it can never touch a
    row another writer already advanced, resigned or expired.  Chooses
    ttl_expired vs idle_expired from which deadline actually lapsed.
    Returns True when the row was expired here.
    """
    now = utcnow()
    stmt = (
        update(HumanGame)
        .where(
            HumanGame.id == game_id,
            HumanGame.status == "ACTIVE",
            HumanGame.engine_pending.is_(True),
            HumanGame.revision == expected_revision,
            or_(
                HumanGame.expires_at <= now,
                HumanGame.idle_expires_at <= now,
            ),
        )
        .values(
            status="EXPIRED",
            termination=case(
                (HumanGame.expires_at <= now, "ttl_expired"),
                else_="idle_expired",
            ),
            result=None,
            engine_pending=False,
        )
    )
    result = session.execute(
        stmt, execution_options={"synchronize_session": False}
    )
    session.commit()
    session.expire_all()
    return result.rowcount == 1


def service_pending_move(settings, session, game: HumanGame) -> str:
    """Execute one owed engine move for ``game`` and persist it.

    Returns a short action description for the worker log.

    Concurrency contract (P1-3 repair): the engine search runs for up to
    ~movetime seconds OUTSIDE any transaction.  During that window the
    browser may resign, the game may expire, or another writer may touch
    the row.  The persisted move therefore uses a compare-and-swap UPDATE
    conditioned on the exact (status=ACTIVE, engine_pending=true,
    revision=expected) state observed BEFORE the search; a late bestmove
    that loses the CAS is discarded, never appended to a resigned/expired
    game and never overwrites its result.
    """
    game = session.get(HumanGame, game.id)
    if game is None or game.status != "ACTIVE" or not game.engine_pending:
        return "human-move skipped (game no longer pending)"
    if game_is_stale(game):
        game.status = "EXPIRED"
        game.termination = (
            "ttl_expired" if _past(game.expires_at) else "idle_expired"
        )
        game.engine_pending = False
        game.result = None
        session.commit()
        return "human-move skipped (game expired)"

    expected_revision = game.revision or 0
    game_id = game.id
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
            _fail_game(session, game_id, expected_revision, "corrupt history")
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
        if not _fail_game(session, game_id, expected_revision, str(exc)):
            # Another writer already advanced this game (duplicate
            # servicing, a resign, or a successful bestmove landed while
            # we were searching); their transition stands.
            session.rollback()
            return (
                "human-move skipped (state changed while engine was thinking)"
            )
        return f"human-move engine failed: {exc}"

    # P2-3 repair: a malformed bestmove (e.g. "bestmove garbage") must
    # terminalize the game, not bubble up as an unhandled ValueError that
    # leaves engine_pending=true and hot-loops the worker.
    try:
        move = chess.Move.from_uci(best_uci)
    except ValueError:
        if not _fail_game(
            session, game_id, expected_revision,
            f"malformed bestmove: {best_uci!r}",
        ):
            session.rollback()
            return (
                "human-move skipped (state changed while engine was thinking)"
            )
        return f"human-move engine failed: malformed bestmove {best_uci!r}"
    if move not in board.legal_moves:
        # A frozen engine replying outside the rules is treated as an engine
        # failure, never silently substituted.
        if not _fail_game(
            session, game_id, expected_revision,
            f"illegal bestmove: {best_uci}",
        ):
            session.rollback()
            return (
                "human-move skipped (state changed while engine was thinking)"
            )
        return "human-move engine failed: illegal bestmove"

    san = board.san(move)
    board.push(move)
    ply = len(moves) + 1
    fen_after = board.fen()
    outcome = board.outcome()

    # --- CAS: only the writer that still owns the pre-search state wins.
    # The deadline columns are part of the ownership token: a bestmove that
    # crosses the idle/TTL deadline mid-search (movetime <= 3s window) must
    # NOT land and re-arm idle_expires_at, resurrecting a dead game.
    now = utcnow()

    cas = update(HumanGame).where(
        HumanGame.id == game_id,
        HumanGame.status == "ACTIVE",
        HumanGame.engine_pending.is_(True),
        HumanGame.revision == expected_revision,
        HumanGame.expires_at > now,
        HumanGame.idle_expires_at > now,
    )
    if outcome is not None:
        new_status = "FINISHED"
        new_result = outcome.result()
        new_termination = (
            outcome.termination.name.lower()
            if outcome.termination is not None
            else "adjudicated"
        )
    else:
        new_status = "ACTIVE"
        new_result = None
        new_termination = None
    result = session.execute(
        cas.values(
            current_fen=fen_after,
            engine_pending=False,
            revision=expected_revision + 1,
            last_move_at=now,
            idle_expires_at=now
            + _timedelta(settings.human_play_idle_seconds),
            status=new_status,
            result=new_result,
            termination=new_termination,
        ),
        execution_options={"synchronize_session": False},
    )
    if result.rowcount != 1:
        # Lost the race (resign / duplicate servicing / a deadline that
        # passed while the engine was thinking).  Discard the late
        # bestmove entirely; if the only reason we lost is that the game
        # went stale mid-search, terminalize it as EXPIRED here rather
        # than leaving a dead ACTIVE+pending row behind.
        session.rollback()
        _expire_if_stale_owner(session, game_id, expected_revision)
        return (
            "human-move skipped (state changed while engine was thinking)"
        )

    session.add(
        HumanGameMove(
            human_game_id=game_id,
            ply=ply,
            side="engine",
            uci=best_uci,
            san=san,
            fen_after=fen_after,
            engine_ms=elapsed_ms,
        )
    )
    session.commit()
    session.expire_all()
    return (
        f"human-move played: game={game_id} ply={ply} uci={best_uci}"
    )


def _timedelta(seconds: int):
    from datetime import timedelta

    return timedelta(seconds=seconds)
