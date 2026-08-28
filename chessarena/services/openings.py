"""Opening resolution for tournaments (P4.F1 Phase C).

Supports legacy single-line EPD sets and PGN books (official Stockfish
suites): a PGN book entry is replayed to a chosen number of plies with
python-chess to produce the starting FEN.  Selection is deterministic for a
frozen ``opening_seed`` — random sampling without replacement from the
eligible pool (entries with at least the requested plies).
"""

from __future__ import annotations

import hashlib
import random
from pathlib import Path

import chess
import chess.pgn

from .cutechess import CutechessLaunchError


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_opening_file_identity(opening_set, snapshot_opening) -> None:
    """Re-hash the ACTUAL opening file and fail closed (P4.F1 P1-3).

    Compares against both the frozen tournament snapshot SHA and the
    registered OpeningSet SHA — a tampered-on-disk file whose DB row was not
    touched must be rejected by the scheduler before Popen and by the
    verifier after pair completion.
    """
    path = Path(opening_set.file_path)
    if not path.is_file():
        raise CutechessLaunchError(f"opening set file missing: {path}")
    actual = sha256_file(path)
    if snapshot_opening.get("sha256") and actual != snapshot_opening["sha256"]:
        raise CutechessLaunchError(
            "opening file SHA does not match the frozen tournament snapshot"
        )
    if opening_set.sha256 and actual != opening_set.sha256:
        raise CutechessLaunchError(
            "opening file SHA does not match the registered OpeningSet"
        )


def _format(opening_set) -> str:
    return (opening_set.manifest or {}).get("format") or opening_set.format


def _epd_fen_for_index(opening_set, opening_index: int) -> str:
    lines = [
        ln.strip()
        for ln in Path(opening_set.file_path).read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]
    if opening_index >= len(lines):
        raise CutechessLaunchError(
            f"opening_index {opening_index} out of range ({len(lines)} lines)"
        )
    # Canonicalize so scheduler-written opening.epd and the verifier's
    # expected FEN match what cutechess emits into the PGN [FEN] header.
    return chess.Board(lines[opening_index].split(";")[0].strip()).fen()


def _iter_games(path: Path):
    with open(path, encoding="utf-8", errors="replace") as fh:
        while True:
            game = chess.pgn.read_game(fh)
            if game is None:
                return
            yield game


def _game_fen(game, plies: int) -> str:
    moves = list(game.mainline_moves())
    if len(moves) < plies:
        raise CutechessLaunchError(
            f"opening shorter than requested {plies} plies ({len(moves)} available)"
        )
    board = game.board()
    for move in moves[:plies]:
        board.push(move)
    return board.fen()


def _pgn_fen_for_index(opening_set, opening_index: int, plies: int) -> str:
    for i, game in enumerate(_iter_games(Path(opening_set.file_path))):
        if i == opening_index:
            return _game_fen(game, plies)
    raise CutechessLaunchError(
        f"opening_index {opening_index} out of range"
    )


def opening_fen_for_index(
    opening_set, opening_index: int, plies: int | None = None
) -> str:
    """The starting FEN for one opening entry (EPD line or replayed PGN)."""
    if _format(opening_set) == "pgn":
        if plies is None:
            raise CutechessLaunchError("plies required for PGN opening sets")
        return _pgn_fen_for_index(opening_set, opening_index, plies)
    return _epd_fen_for_index(opening_set, opening_index)


def eligible_openings(opening_set, plies: int | None = None) -> list[int]:
    """Indices of openings with at least ``plies`` moves (all entries for
    EPD sets, which have no depth notion)."""
    if _format(opening_set) == "pgn":
        eligible: list[int] = []
        for i, game in enumerate(_iter_games(Path(opening_set.file_path))):
            if len(list(game.mainline_moves())) >= (plies or 0):
                eligible.append(i)
        return eligible
    return list(range(opening_set.position_count))


def _eligible_fens_by_index(opening_set, plies: int | None, eligible_indices: list[int]):
    """Starting FEN of every eligible opening, computed in ONE pass (S4.3D:
    exclusion lists must not re-parse the book per index)."""
    if _format(opening_set) == "pgn":
        eligible = set(eligible_indices)
        out: dict[int, str] = {}
        for i, game in enumerate(_iter_games(Path(opening_set.file_path))):
            if i in eligible:
                out[i] = _game_fen(game, plies)
        return out
    return {i: _epd_fen_for_index(opening_set, i) for i in eligible_indices}


def select_opening_indices(
    opening_set, count: int, plies: int | None, seed: int,
    exclude_fens: list[str] | None = None,
) -> list[int]:
    """Deterministic sample without replacement from the eligible pool.

    The same (opening_set, plies, seed, count) always yields the same
    indices; indices are stable across runs for a frozen snapshot.
    ``exclude_fens`` (normalized starting FENs) removes openings already used
    by earlier tournaments from the pool (S4.3D: fresh formal-SPRT openings).
    """
    pool = eligible_openings(opening_set, plies)
    if exclude_fens and any(f and f.strip() for f in exclude_fens):
        excluded = {f.strip() for f in exclude_fens if f and f.strip()}
        fens_by_index = _eligible_fens_by_index(opening_set, plies, pool)
        pool = [i for i in pool if fens_by_index.get(i) not in excluded]
    if len(pool) < count:
        raise CutechessLaunchError(
            f"opening book has only {len(pool)} eligible openings "
            f"(requested {plies} plies); need {count}"
        )
    return random.Random(seed).sample(pool, count)


def opening_fens_for_indices(
    opening_set, indices: list[int], plies: int | None = None
) -> list[str]:
    """Canonical starting FENs for the given indices, in ONE pass.

    Used by the formal-experiment planner to rebuild a prior run's frozen
    opening sample from its snapshot (opening_set_id + indices + plies).
    The caller is responsible for verifying file/snapshot identity first
    (``verify_opening_file_identity``); this helper only resolves FENs.
    """
    fmt = _format(opening_set)
    wanted = sorted(set(indices))
    out: dict[int, str] = {}
    if fmt == "pgn":
        if plies is None:
            raise CutechessLaunchError("plies required for PGN opening sets")
        remaining = set(wanted)
        for i, game in enumerate(_iter_games(Path(opening_set.file_path))):
            if not remaining:
                break
            if i in remaining:
                out[i] = _game_fen(game, plies)
                remaining.discard(i)
        missing = remaining - set(out)
        if missing:
            raise CutechessLaunchError(
                f"opening indices out of range: {sorted(missing)[:5]}"
            )
    else:
        lines = [
            ln.strip()
            for ln in Path(opening_set.file_path).read_text(
                encoding="utf-8").splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")
        ]
        for i in wanted:
            if i >= len(lines):
                raise CutechessLaunchError(
                    f"opening_index {i} out of range ({len(lines)} lines)"
                )
            out[i] = chess.Board(lines[i].split(";")[0].strip()).fen()
    return [out[i] for i in indices]


def verify_prior_opening_snapshot(
    opening_set, snapshot_opening: dict
) -> None:
    """Fail-closed identity check for a prior run used as an exclusion
    source: the frozen snapshot must carry an opening identity, the
    registry row must exist and match the snapshot SHA, and the ACTUAL
    file on disk must still hash to the same SHA."""
    from .cutechess import CutechessLaunchError

    if not snapshot_opening or not snapshot_opening.get("sha256"):
        raise CutechessLaunchError(
            "prior run snapshot lacks a frozen opening sha256"
        )
    if snapshot_opening.get("opening_set_id") != opening_set.opening_set_id:
        raise CutechessLaunchError(
            "prior run snapshot opening_set_id does not match the registry"
        )
    if opening_set.sha256 and \
            snapshot_opening["sha256"] != opening_set.sha256:
        raise CutechessLaunchError(
            "prior run snapshot opening sha256 does not match the "
            "registered OpeningSet (registry changed since the run)"
        )
    path = Path(opening_set.file_path)
    if not path.is_file():
        raise CutechessLaunchError(f"opening set file missing: {path}")
    actual = sha256_file(path)
    if actual != snapshot_opening["sha256"]:
        raise CutechessLaunchError(
            "opening file on disk does not match the prior run snapshot"
        )
