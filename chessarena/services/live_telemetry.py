"""P4.11 live telemetry: parse cutechess ``-debug`` transport into live state.

Cutechess prints every engine input/output line as ``>Name(game): payload``
(cutechess -> engine) or ``<Name(game): payload`` (engine -> cutechess).
From that stream we derive the real current position (the ``position``
command carries the full move sequence from the opening FEN), both engines'
clocks (the ``go wtime/btime`` line), each engine's latest self-evaluation
(``info ... score/depth/nodes/nps/time/pv``) and the last move (``bestmove``).

Only whitelisted, sanitized values ever leave this module; the raw UCI log
is never exposed.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

import chess

# >Name(0): payload  /  <Name(0): payload  (name may contain spaces).
_DEBUG_RE = re.compile(r"^([<>])(.+?)\((\d+)\): (.*)$")

# Hard cap on the tail read: keeps repeated 1.5s polls cheap while always
# covering the currently streaming search.
_TAIL_BYTES = 1_000_000


def _read_tail(path: Path) -> str:
    data = path.read_bytes()
    if len(data) > _TAIL_BYTES:
        data = data[-_TAIL_BYTES:]
    return data.decode("utf-8", errors="replace")


def parse_live_state(stdout_path: Path) -> dict:
    """Current live state from a pair run's stdout.log.

    Returns:
      current_fen, side_to_move, last_move, ply, game (0-based debug game),
      engines: {name: {eval_cp, mate, depth, nodes, nps, time_ms, pv}},
      clocks:  {name: {"own_ms", "opp_ms"}} from the last ``go`` each engine
               received (own_ms is the engine's remaining clock).
    """
    state: dict = {
        "current_fen": None,
        "side_to_move": None,
        "last_move": None,
        "ply": None,
        "game": None,
        "engines": {},
        "clocks": {},
    }
    if not stdout_path.is_file():
        return state

    position_fen: Optional[str] = None
    position_moves: list[str] = []
    for line in _read_tail(stdout_path).splitlines():
        m = _DEBUG_RE.match(line)
        if not m:
            continue
        direction, name, game, payload = (
            m.group(1), m.group(2), int(m.group(3)), m.group(4),
        )
        state["game"] = game
        if direction == ">":
            if payload.startswith("position"):
                tokens = payload.split()
                if len(tokens) >= 2 and tokens[1] == "startpos":
                    position_fen = None
                elif len(tokens) >= 3 and tokens[1] == "fen":
                    position_fen = tokens[2]
                else:
                    continue
                position_moves = []
                if "moves" in tokens:
                    position_moves = tokens[tokens.index("moves") + 1:]
            elif payload.startswith("go"):
                clocks: dict[str, int] = {}
                tokens = payload.split()
                for i, tok in enumerate(tokens):
                    if tok in ("wtime", "btime", "winc", "binc") and i + 1 < len(tokens):
                        try:
                            clocks[tok] = int(tokens[i + 1])
                        except ValueError:
                            pass
                if "wtime" in clocks:
                    state["clocks"][name] = {
                        "own_ms": clocks["wtime"],
                        "opp_ms": clocks.get("btime"),
                    }
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
                            try:
                                score_cp = int(tokens[i + 2])
                            except ValueError:
                                pass
                        elif tokens[i + 1] == "mate":
                            try:
                                mate = int(tokens[i + 2])
                            except ValueError:
                                pass
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
                    # Keep the latest evaluation for this engine.
                    state["engines"][name] = {
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

    try:
        board = chess.Board(position_fen) if position_fen else chess.Board()
        for uci in position_moves:
            board.push_uci(uci)
        state["current_fen"] = board.fen()
        state["side_to_move"] = "w" if board.turn == chess.WHITE else "b"
        state["ply"] = len(position_moves)
    except (ValueError, IndexError):
        pass
    if state["last_move"] is None and position_moves:
        state["last_move"] = position_moves[-1]
    return state


def _int_or(value: str) -> Optional[int]:
    try:
        return int(value)
    except ValueError:
        return None
