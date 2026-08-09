"""P4.11 live telemetry: parse cutechess ``-debug`` transport into live state.

Cutechess prints every engine input/output line as
``<counter> >Name(index): payload`` (cutechess -> engine) or
``<counter> <Name(index): payload`` (engine -> cutechess); the ``(index)`` is
the ENGINE INDEX (0 = engine A instance, 1 = engine B instance), never a game
number.  From that stream we derive the real current position (the full
``position fen`` carries all six FEN fields plus the move list), both clocks
(the latest ``go wtime/btime`` — absolute clocks of White/Black — adjusted by
the active engine's ``info time``), each engine's latest self-evaluation and
the last move (``bestmove``).

Engine identity is the engine INDEX so intentional self-play with identical
display names cannot merge the two instances.  A new ``Started game N of M``
line resets the per-game telemetry (position, clocks, evals, last move).

Only whitelisted, sanitized values ever leave this module.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

import chess

# <counter> >Name(index): payload  /  <counter> <Name(index): payload
_DEBUG_RE = re.compile(r"^\s*\d*\s*([<>])(.+?)\((\d+)\): (.*)$")
_STARTED_RE = re.compile(r"^Started game (\d+) of (\d+)")
_FINISHED_RE = re.compile(r"^Finished game (\d+) \((.+?)\): (\S+)")

# Hard cap on the tail read; the file is opened and seeked so a 50MB
# -debug log is never read in full on every 1.5s poll.  The window covers a
# typical game's full traffic (a blitz game is well under 2MB of debug
# lines), so the current game's boundary line stays visible; when the log
# outgrows the window the live page fails closed (no game badge, no side
# boards) rather than guess.
_TAIL_BYTES = 2_000_000


def is_debug_transport_line(line: str) -> bool:
    """True for cutechess ``-debug`` transport lines (with optional message
    counter prefix).  Shared by the verifier and the telemetry parser so the
    two can never drift."""
    return bool(_DEBUG_RE.match(line))


def _read_tail(path: Path) -> str:
    with path.open("rb") as fh:
        fh.seek(0, 2)  # end
        size = fh.tell()
        fh.seek(max(0, size - _TAIL_BYTES))
        data = fh.read()
    return data.decode("utf-8", errors="replace")


def _empty_engines() -> dict:
    return {0: {}, 1: {}}


def parse_live_state(stdout_path: Path) -> dict:
    """Current live state from a pair run's stdout.log.

    Returns:
      current_fen, side_to_move, last_move, ply, game_in_pair (1-based),
      state (pending | game_running | pair_done), last_result,
      has_debug (a -debug stream is present in the read window),
      engines: {index: {eval_cp, mate, depth, nodes, nps, time_ms, pv}},
      go:      {"wtime": ms, "btime": ms} from the LATEST ``go`` (White's and
               Black's absolute clocks), active_engine: index of the engine
               that received that go (the side to move).
    """
    state: dict = {
        "current_fen": None,
        "side_to_move": None,
        "last_move": None,
        "ply": None,
        "game_in_pair": None,
        "state": "pending",
        "last_result": None,
        "has_debug": False,
        "engines": _empty_engines(),
        "go": {},
        "active_engine": None,
    }
    if not stdout_path.is_file():
        return state

    position_fen: Optional[str] = None
    position_moves: list[str] = []
    finished: dict[int, str] = {}
    total_games: int = 2
    debug_lines = 0
    for line in _read_tail(stdout_path).splitlines():
        m = _STARTED_RE.match(line)
        if m:
            # New game boundary: reset all per-game telemetry.
            state["game_in_pair"] = int(m.group(1))
            total_games = int(m.group(2))
            state["current_fen"] = None
            state["side_to_move"] = None
            state["last_move"] = None
            state["ply"] = None
            state["engines"] = _empty_engines()
            state["go"] = {}
            state["active_engine"] = None
            position_fen = None
            position_moves = []
            continue
        m = _FINISHED_RE.match(line)
        if m:
            finished[int(m.group(1))] = m.group(3)
            continue
        m = _DEBUG_RE.match(line)
        if not m:
            continue
        debug_lines += 1
        direction, name, idx, payload = (
            m.group(1), m.group(2), int(m.group(3)), m.group(4),
        )
        if direction == ">":
            if payload.startswith("position"):
                tokens = payload.split()
                if len(tokens) >= 2 and tokens[1] == "startpos":
                    position_fen = None
                elif len(tokens) >= 3 and tokens[1] == "fen":
                    # Full six-field FEN: <pieces> <side> <castling> <ep>
                    # <halfmove> <fullmove> [moves ...]
                    moves_at = (
                        tokens.index("moves")
                        if "moves" in tokens else len(tokens)
                    )
                    position_fen = " ".join(tokens[2:moves_at])
                else:
                    continue
                position_moves = (
                    tokens[tokens.index("moves") + 1:]
                    if "moves" in tokens else []
                )
                # The go command for this position arrives right after; the
                # receiver is the side to move, so compute it now.
                try:
                    board = (
                        chess.Board(position_fen)
                        if position_fen else chess.Board()
                    )
                    for uci in position_moves:
                        board.push_uci(uci)
                    state["side_to_move"] = (
                        "w" if board.turn == chess.WHITE else "b"
                    )
                    state["current_fen"] = board.fen()
                    state["ply"] = len(position_moves)
                except (ValueError, IndexError):
                    pass
            elif payload.startswith("go"):
                clocks: dict[str, int] = {}
                tokens = payload.split()
                for i, tok in enumerate(tokens):
                    if tok in ("wtime", "btime", "winc", "binc") and i + 1 < len(tokens):
                        try:
                            clocks[tok] = int(tokens[i + 1])
                        except ValueError:
                            pass
                # The latest go carries BOTH sides' absolute clocks.
                if "wtime" in clocks:
                    state["go"] = {
                        "wtime": clocks["wtime"],
                        "btime": clocks["btime"],
                    }
                    state["active_engine"] = idx
        else:
            if payload.startswith("info"):
                tokens = payload.split()
                score_cp: Optional[int] = None
                mate: Optional[int] = None
                depth = nodes = nps = time_ms = None
                pv: list[str] = []
                for i, tok in enumerate(tokens):
                    if tok == "score" and i + 2 < len(tokens):
                        if tokens[i + 1] == "cp":
                            score_cp = _int_or(tokens[i + 2])
                        elif tokens[i + 1] == "mate":
                            mate = _int_or(tokens[i + 2])
                    elif tok == "depth" and i + 1 < len(tokens):
                        depth = _int_or(tokens[i + 1])
                    elif tok == "nodes" and i + 1 < len(tokens):
                        nodes = _int_or(tokens[i + 1])
                    elif tok == "nps" and i + 1 < len(tokens):
                        nps = _int_or(tokens[i + 1])
                    elif tok == "time" and i + 1 < len(tokens):
                        time_ms = _int_or(tokens[i + 1])
                    elif tok == "pv":
                        pv = tokens[i + 1:]
                if score_cp is not None or mate is not None:
                    # Keep the latest evaluation for this engine instance.
                    state["engines"][idx] = {
                        "eval_cp": score_cp,
                        "mate": mate,
                        "depth": depth,
                        "nodes": nodes,
                        "nps": nps,
                        "time_ms": time_ms,
                        "pv": pv,
                    }
            elif payload.startswith("bestmove"):
                parts = payload.split()
                if len(parts) >= 2:
                    state["last_move"] = parts[1]

    if position_moves and state["last_move"] is None:
        state["last_move"] = position_moves[-1]
    state["has_debug"] = debug_lines > 0
    if finished:
        state["last_result"] = finished[max(finished)]
    if len(finished) >= total_games:
        state["state"] = "pair_done"
    elif state["game_in_pair"] is not None:
        state["state"] = "game_running"
    return state


def _int_or(value: str) -> Optional[int]:
    try:
        return int(value)
    except ValueError:
        return None
