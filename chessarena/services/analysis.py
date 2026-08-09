"""Post-game Stockfish analysis (P4.7 v1).

Analysis is an artifact-only feature: the admin writes a ``<game_id>.request.json``
under ``run_root/<tournament_id>/analysis/`` and the worker, when no match work
exists, analyzes the game with the registered unrestricted Stockfish build and
writes ``<game_id>.json`` (or ``<game_id>.error.json`` on failure).  No DB
migration, no queue infrastructure, no new analysis engine registry.

Contract (frozen from day one):

- every position from ply 0 (the game's opening position) through the final
  position is analyzed with a fixed node budget (100000 nodes),
- scores are always White perspective: ``score_cp > 0`` means White is better,
  ``mate > 0`` means White mates, ``mate < 0`` means Black mates,
- the artifact records which build/hash produced the numbers.
"""

from __future__ import annotations

import io
import json
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Optional

import chess
import chess.pgn

from ..config import Settings
from ..models import COMPLETED, EngineBuild, Game, Tournament, utcnow
from ..services import artifacts
from .replay import ReplayError, read_single_game_pgn

SCHEMA_VERSION = 1
ANALYSIS_NODES = 100_000
ANALYSIS_THREADS = 2
ANALYSIS_HASH_MB = 256
# Wall-clock deadline per UCI handshake/search wait: a hung analyzer must
# never block the worker from servicing matches (P4.7 repair).
SEARCH_TIMEOUT = 30.0


class AnalysisError(Exception):
    pass


def analysis_dir(tournament_id: str) -> Path:
    return artifacts.tournament_run_dir(tournament_id) / "analysis"


def request_path(tournament_id: str, game_id: str) -> Path:
    return analysis_dir(tournament_id) / f"{game_id}.request.json"


def result_path(tournament_id: str, game_id: str) -> Path:
    return analysis_dir(tournament_id) / f"{game_id}.json"


def error_path(tournament_id: str, game_id: str) -> Path:
    return analysis_dir(tournament_id) / f"{game_id}.error.json"


def analysis_state(game: Game) -> str:
    """not_requested | queued | ready | failed — derived purely from artifacts."""
    req = request_path(game.tournament_id, game.id)
    if not req.is_file():
        return "not_requested"
    if result_path(game.tournament_id, game.id).is_file():
        return "ready"
    if error_path(game.tournament_id, game.id).is_file():
        return "failed"
    return "queued"


def request_analysis(settings: Settings, game: Game) -> None:
    """Write (or overwrite for re-analysis) the request artifact."""
    req = request_path(game.tournament_id, game.id)
    req.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "game_id": game.id,
        "requested_at": utcnow().isoformat(),
    }
    tmp = req.with_suffix(".request.json.tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    tmp.replace(req)
    # Clear stale outcomes so a re-analyze is unambiguous.
    for stale in (result_path(game.tournament_id, game.id),
                  error_path(game.tournament_id, game.id)):
        if stale.exists():
            stale.unlink()


def next_request(session, settings: Settings) -> Optional[Game]:
    """The oldest verified game in a COMPLETED match whose request has not yet
    produced a result or an error."""
    games = (
        session.query(Game)
        .join(Tournament, Tournament.id == Game.tournament_id)
        .filter(Tournament.status == COMPLETED, Game.verified.is_(True))
        .order_by(Game.finished_at.asc())
        .all()
    )
    for game in games:
        req = request_path(game.tournament_id, game.id)
        if not req.is_file():
            continue
        if result_path(game.tournament_id, game.id).is_file():
            continue
        if error_path(game.tournament_id, game.id).is_file():
            continue
        return game
    return None


def run_analysis(settings: Settings, session, game: Game) -> str:
    """Analyze one game and write the artifact.  Returns 'completed' | 'failed'
    | 'abandoned' (match deleted while analyzing; nothing is written)."""
    try:
        _analyze(settings, session, game)
        return "completed"
    except AnalysisAbandoned:
        return "abandoned"
    except Exception as exc:  # noqa: BLE001 - artifact must record any failure
        if not _match_still_exists(session, game):
            # The match was deleted while analyzing: never recreate orphan
            # artifacts under a run dir that no longer belongs to a match.
            return "abandoned"
        payload = {
            "schema_version": SCHEMA_VERSION,
            "game_id": game.id,
            "error": str(exc),
            "failed_at": utcnow().isoformat(),
        }
        err = error_path(game.tournament_id, game.id)
        err.parent.mkdir(parents=True, exist_ok=True)
        err.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return "failed"


class AnalysisAbandoned(AnalysisError):
    """The match disappeared while its analysis was in flight."""


def _match_still_exists(session, game: Game) -> bool:
    row = session.query(Game).filter(Game.id == game.id).first()
    if row is None or not row.verified:
        return False
    tournament = (
        session.query(Tournament)
        .filter(Tournament.id == game.tournament_id)
        .first()
    )
    if tournament is None or tournament.status != COMPLETED:
        return False
    if not analysis_dir(game.tournament_id).is_dir():
        return False
    if not request_path(game.tournament_id, game.id).is_file():
        return False
    return True


def _analyze(settings: Settings, session, game: Game) -> None:
    if not game.verified:
        raise AnalysisError("game is not verified")
    tournament = (
        session.query(Tournament)
        .filter(Tournament.id == game.tournament_id)
        .first()
    )
    if tournament is None or tournament.status != COMPLETED:
        raise AnalysisError("match is not completed")

    build = (
        session.query(EngineBuild)
        .filter(
            EngineBuild.enabled.is_(True),
            EngineBuild.engine_name == "Stockfish",
        )
        .order_by(EngineBuild.created_at.desc())
        .first()
    )
    if build is None:
        raise AnalysisError("no registered Stockfish build to analyze with")
    binary = Path(build.binary_path)
    if not binary.is_file():
        raise AnalysisError(f"analyzer binary missing: {binary}")

    try:
        pgn_text = read_single_game_pgn(game)
    except ReplayError as exc:
        raise AnalysisError(str(exc)) from exc
    game_pgn = chess.pgn.read_game(io.StringIO(pgn_text))
    if game_pgn is None:
        raise AnalysisError("unparsable PGN")

    fen_header = game_pgn.headers.get("FEN")
    board = chess.Board(fen_header) if fen_header else chess.Board()
    fens = [board.fen()]
    for move in game_pgn.mainline_moves():
        board.push(move)
        fens.append(board.fen())

    results = _probe_positions(binary, fens)
    positions = [
        {
            "ply": idx,
            "fen": fen,
            "score_cp": res.get("score_cp"),
            "mate": res.get("mate"),
            "best_move": res.get("best_move"),
            "pv": res.get("pv") or [],
        }
        for idx, (fen, res) in enumerate(zip(fens, results))
    ]
    artifact = {
        "schema_version": SCHEMA_VERSION,
        "game_id": game.id,
        "engine": {
            "name": build.engine_name,
            "build_id": build.build_id,
            "binary_sha256": build.binary_sha256,
        },
        "limit": {"type": "nodes", "value": ANALYSIS_NODES},
        "positions": positions,
    }
    out = result_path(game.tournament_id, game.id)
    if not _match_still_exists(session, game):
        # The match (or its run dir) was deleted while analyzing: do not
        # recreate artifacts for a match that no longer exists.
        raise AnalysisAbandoned()
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")
    tmp.replace(out)


def read_analysis(game: Game) -> Optional[dict]:
    """The validated analysis artifact, or None when the game has no result.
    Raises AnalysisError when the artifact exists but is malformed."""
    out = result_path(game.tournament_id, game.id)
    if not out.is_file():
        return None
    try:
        data = json.loads(out.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AnalysisError(f"analysis artifact unreadable: {exc}") from exc
    if data.get("schema_version") != SCHEMA_VERSION:
        raise AnalysisError("unsupported analysis schema version")
    if data.get("game_id") != game.id:
        raise AnalysisError("analysis artifact game_id mismatch")
    if not isinstance(data.get("positions"), list):
        raise AnalysisError("analysis artifact has no positions")
    return data


def _probe_positions(binary: Path, fens: list[str]) -> list[dict]:
    """One Stockfish process, one fixed-node search per position.  Scores are
    converted to White perspective.  Every wait has a wall-clock deadline;
    on timeout the process is terminated and AnalysisError raised."""
    proc = subprocess.Popen(
        [str(binary)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )
    reader = _line_reader(proc)
    try:
        _write_and_wait(proc, reader, "uci", "uciok")
        _write_and_wait(proc, reader, "isready", "readyok")
        for name, value in (
            ("UCI_LimitStrength", "false"),
            ("Threads", str(ANALYSIS_THREADS)),
            ("Hash", str(ANALYSIS_HASH_MB)),
        ):
            proc.stdin.write(f"setoption name {name} value {value}\n")
            proc.stdin.flush()
        _write_and_wait(proc, reader, "isready", "readyok")

        results: list[dict] = []
        for fen in fens:
            board = chess.Board(fen)
            white_to_move = board.turn == chess.WHITE
            # ucinewgame is silent in UCI: write it and sync with isready.
            proc.stdin.write("ucinewgame\n")
            proc.stdin.flush()
            _write_and_wait(proc, reader, "isready", "readyok")
            proc.stdin.write(f"position fen {fen}\n")
            proc.stdin.flush()
            proc.stdin.write(f"go nodes {ANALYSIS_NODES}\n")
            proc.stdin.flush()
            score_cp: Optional[int] = None
            mate: Optional[int] = None
            pv: list[str] = []
            best_move: Optional[str] = None
            for line in _read_until(reader, "bestmove"):
                tokens = line.strip().split()
                if len(tokens) >= 2 and tokens[0] == "bestmove":
                    best_move = tokens[1]
                    continue
                if not tokens or tokens[0] != "info":
                    continue
                score = mate_score = None
                for i, tok in enumerate(tokens):
                    if tok == "score" and i + 2 < len(tokens):
                        if tokens[i + 1] == "cp":
                            score = int(tokens[i + 2])
                        elif tokens[i + 1] == "mate":
                            mate_score = int(tokens[i + 2])
                    elif tok == "pv" and i + 1 < len(tokens):
                        pv = tokens[i + 1:]
                if score is not None or mate_score is not None:
                    # Keep the final search evaluation.
                    score_cp = score
                    mate = mate_score
            if not white_to_move:
                if score_cp is not None:
                    score_cp = -score_cp
                if mate is not None:
                    mate = -mate
            if not pv and best_move:
                pv = [best_move]
            results.append(
                {
                    "score_cp": score_cp,
                    "mate": mate,
                    "best_move": best_move,
                    "pv": pv,
                }
            )
        return results
    finally:
        _terminate(proc)


def _line_reader(proc: subprocess.Popen) -> queue.Queue:
    """A daemon thread forwards stdout lines into a queue so waits can time
    out (readline itself would block forever on a hung engine)."""
    q: queue.Queue = queue.Queue()

    def _run() -> None:
        try:
            for line in proc.stdout:
                q.put(line)
        except Exception as exc:  # noqa: BLE001 - surfaced to the waiter
            q.put(exc)
        finally:
            q.put(None)

    threading.Thread(target=_run, name="analysis-reader", daemon=True).start()
    return q


def _terminate(proc: subprocess.Popen) -> None:
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


def _write_and_wait(proc: subprocess.Popen, reader, cmd: str,
                    terminator: str) -> None:
    proc.stdin.write(cmd + "\n")
    proc.stdin.flush()
    for _ in _read_until(reader, terminator):
        pass


def _read_until(reader: queue.Queue, terminator: str,
                timeout: float = SEARCH_TIMEOUT) -> list[str]:
    lines: list[str] = []
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AnalysisError(
                f"analyzer timed out waiting for {terminator!r}"
            )
        try:
            item = reader.get(timeout=remaining)
        except queue.Empty:
            continue
        if item is None:
            raise AnalysisError("analyzer closed output unexpectedly")
        if isinstance(item, Exception):
            raise AnalysisError(f"analyzer read failed: {item}")
        lines.append(item)
        if terminator in item:
            return lines
