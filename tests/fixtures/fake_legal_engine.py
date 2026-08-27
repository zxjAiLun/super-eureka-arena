#!/usr/bin/env python3
"""Fake UCI engine that plays a legal move for any position.

Used by the human-play tests: unlike fake_uci_engine.py (which always
answers e2e4), this one parses ``position startpos moves ...`` /
``position fen ...``, computes the legal moves with python-chess and picks
a deterministic one (the first in SAN order, or the last when the env
FAKE_UCI_PLAY_LAST=1), so games can run to a natural terminal state.
"""

from __future__ import annotations

import os
import sys

import chess


def pick_move(board: chess.Board) -> chess.Move:
    moves = list(board.legal_moves)
    if not moves:
        raise SystemExit(1)
    if os.environ.get("FAKE_UCI_PLAY_LAST") == "1":
        return moves[-1]
    return moves[0]


def main() -> int:
    elo_min = os.environ.get("FAKE_UCI_ELO_MIN", "-200")
    elo_max = os.environ.get("FAKE_UCI_ELO_MAX", "2850")
    lines = [
        "id name FakeLegalEngine 1.0",
        "option name UCI_LimitStrength type check default false",
        f"option name UCI_Elo type spin default 1350 min {elo_min} max {elo_max}",
        "option name Hash type spin default 16 min 1 max 1024",
        "option name Threads type spin default 1 min 1 max 1",
        "uciok",
    ]
    for line in lines:
        print(line, flush=True)

    board = chess.Board()
    for line in sys.stdin:
        cmd = line.strip()
        if cmd == "isready":
            print("readyok", flush=True)
        elif cmd == "ucinewgame":
            board = chess.Board()
        elif cmd.startswith("position"):
            parts = cmd.split()
            board = chess.Board()
            if "startpos" in parts:
                idx = parts.index("startpos")
                rest = parts[idx + 1:]
                if rest and rest[0] == "moves":
                    for uci in rest[1:]:
                        board.push(chess.Move.from_uci(uci))
            elif "fen" in parts:
                idx = parts.index("fen")
                fen_tokens = []
                for tok in parts[idx + 1:]:
                    if tok == "moves":
                        break
                    fen_tokens.append(tok)
                board = chess.Board(" ".join(fen_tokens))
                mi = parts.index("moves") if "moves" in parts else -1
                if mi != -1:
                    for uci in parts[mi + 1:]:
                        board.push(chess.Move.from_uci(uci))
        elif cmd.startswith("go"):
            if board.is_game_over():
                print("bestmove (none)", flush=True)
                continue
            move = pick_move(board)
            board.push(move)
            print(
                f"info depth 1 seldepth 1 score cp 12 pv {move.uci()}",
                flush=True,
            )
            print(f"bestmove {move.uci()}", flush=True)
        elif cmd == "quit":
            return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
