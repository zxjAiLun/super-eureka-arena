"""Arena Elo (P4.8 v1): anchor-calibrated ratings recomputed from history.

Design:

- Only COMPLETED matches with ``arena_elo_enabled`` that pair one engine
  against a Stockfish anchor participate.  Engine-vs-engine matches are not
  used in v1 (no rating propagation).
- An anchor is a frozen snapshot side whose uci_options carry
  ``UCI_LimitStrength == true`` and an integer ``UCI_Elo`` on a Stockfish
  build; the anchor's Arena Elo is that UCI_Elo.
- A competitor is the frozen engine configuration: binary_sha256 +
  command_args + uci_options hashed together, so different builds/profiles
  never merge even if they share a display name.
- For each time-control pool (bullet_1_0 / blitz_3_2 / rapid_5_3) the engine
  rating solves  sum_i n_i * E(R, a_i) = actual_score  with a binary search,
  so the result depends only on the surviving match history, never on match
  insertion order or a K-factor.
- Ratings are recomputed every request; deleting a match simply removes its
  contribution on the next recompute.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Optional

from ..config import TIME_CONTROLS
from ..models import COMPLETED, EngineBuild, Tournament

PROVISIONAL_GAMES = 50
SEARCH_MARGIN = 1000
ANCHOR_ENGINE_NAME = "Stockfish"


def engine_fingerprint(side: dict) -> str:
    """A competitor is one frozen configuration: binary + command args +
    UCI options."""
    payload = {
        "binary_sha256": (side or {}).get("binary_sha256"),
        "command_args": (side or {}).get("command_args") or [],
        "uci_options": (side or {}).get("uci_options") or {},
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _uci_elo(side: dict) -> Optional[int]:
    opts = (side or {}).get("uci_options") or {}
    limit = opts.get("UCI_LimitStrength")
    elo = opts.get("UCI_Elo")
    if limit is not True and str(limit).lower() != "true":
        return None
    try:
        return int(elo)
    except (TypeError, ValueError):
        return None


def _is_stockfish_build(session, side: dict) -> bool:
    build_id = (side or {}).get("build_id")
    if not build_id:
        return False
    build = (
        session.query(EngineBuild)
        .filter(EngineBuild.build_id == build_id)
        .first()
    )
    # Fail closed: only an actual registered Stockfish build is an anchor.  A
    # missing build row must not be guessed to be Stockfish (some other engine
    # with UCI_LimitStrength/UCI_Elo could then be misread as a fixed anchor).
    return build is not None and build.engine_name == ANCHOR_ENGINE_NAME


def is_anchor(session, side: dict) -> bool:
    return _uci_elo(side) is not None and _is_stockfish_build(session, side)


def anchor_rating(side: dict) -> Optional[int]:
    return _uci_elo(side)


@dataclass
class AnchorMatch:
    anchor_rating: int
    games: int
    score: float  # engine-side points (win=1, draw=0.5)


@dataclass
class RatingEntry:
    fingerprint: str
    display_name: str
    matches: list[AnchorMatch] = field(default_factory=list)

    @property
    def games(self) -> int:
        return sum(m.games for m in self.matches)

    @property
    def actual_score(self) -> float:
        return sum(m.score for m in self.matches)


def _expected_total(entries: list[AnchorMatch], rating: float) -> float:
    total = 0.0
    for m in entries:
        total += m.games / (1 + 10 ** ((m.anchor_rating - rating) / 400))
    return total


def solve_rating(entries: list[AnchorMatch]) -> dict:
    """Binary-search the engine rating whose expected score matches the actual
    score over its anchor matches.  Hitting a search bound means the data only
    bounds the rating (e.g. 20/20 vs one weak anchor)."""
    if not entries:
        return {"rating": None, "lower_bound": False, "upper_bound": False}
    anchors = [m.anchor_rating for m in entries]
    lo = min(anchors) - SEARCH_MARGIN
    hi = max(anchors) + SEARCH_MARGIN
    actual = sum(m.score for m in entries)
    total_games = sum(m.games for m in entries)

    def f(r: float) -> float:
        return _expected_total(entries, r) - actual

    # f is strictly increasing in r.
    if f(lo) > 0:
        return {"rating": int(lo), "lower_bound": True, "upper_bound": False}
    if f(hi) < 0:
        return {"rating": int(hi), "lower_bound": False, "upper_bound": True}
    for _ in range(60):
        mid = (lo + hi) / 2
        if f(mid) < 0:
            lo = mid
        else:
            hi = mid
    rating = int(round((lo + hi) / 2))
    return {"rating": rating, "lower_bound": False, "upper_bound": False}


def compute_ratings(session) -> dict:
    """{time_control: {"engines": [...], "anchors": [...]}} across all rated,
    completed matches.  Deterministic: order-independent by construction."""
    matches = (
        session.query(Tournament)
        .filter(
            Tournament.status == COMPLETED,
            Tournament.arena_elo_enabled.is_(True),
        )
        .all()
    )
    pools: dict[str, dict[str, RatingEntry]] = {
        tc: {} for tc in TIME_CONTROLS
    }
    anchor_pool: dict[str, dict[int, str]] = {tc: {} for tc in TIME_CONTROLS}

    for t in matches:
        tc = t.time_control
        if tc not in pools:
            continue
        snap = t.config_snapshot or {}
        side_a, side_b = snap.get("engine_a") or {}, snap.get("engine_b") or {}
        a_anchor = is_anchor(session, side_a)
        b_anchor = is_anchor(session, side_b)
        if a_anchor == b_anchor:
            continue  # engine-vs-engine or anchor-vs-anchor: not rated in v1
        if a_anchor:
            engine_side, anchor_side = side_b, side_a
            engine_on_a = False
        else:
            engine_side, anchor_side = side_a, side_b
            engine_on_a = True
        a_rating = anchor_rating(anchor_side)
        if a_rating is None:
            continue

        fingerprint = engine_fingerprint(engine_side)
        display = (engine_side.get("display_name")
                   or engine_side.get("preset_id") or "unknown")
        entry = pools[tc].setdefault(
            fingerprint, RatingEntry(fingerprint=fingerprint,
                                     display_name=display)
        )
        # Score from the engine's perspective regardless of A/B placement:
        # engine on A -> candidate wins, engine on B -> candidate losses.
        if engine_on_a:
            score = t.candidate_wins + 0.5 * t.draws
        else:
            score = t.candidate_losses + 0.5 * t.draws
        entry.matches.append(
            AnchorMatch(anchor_rating=a_rating, games=t.requested_pairs * 2,
                        score=score)
        )
        anchor_pool[tc][a_rating] = (anchor_side.get("display_name")
                                     or f"Stockfish {a_rating}")

    result: dict[str, dict] = {}
    for tc, engines in pools.items():
        entries = sorted(
            engines.values(), key=lambda e: e.display_name.lower()
        )
        rows = []
        for e in entries:
            solved = solve_rating(e.matches)
            rows.append(
                {
                    "fingerprint": e.fingerprint,
                    "display_name": e.display_name,
                    "rating": solved["rating"],
                    "lower_bound": solved["lower_bound"],
                    "upper_bound": solved["upper_bound"],
                    "games": e.games,
                    "provisional": e.games < PROVISIONAL_GAMES,
                }
            )
        anchors = [
            {"rating": r, "display_name": name}
            for r, name in sorted(anchor_pool[tc].items())
        ]
        result[tc] = {"engines": rows, "anchors": anchors}
    return result


def engine_rating(session, t: Tournament) -> Optional[dict]:
    """Current Arena Elo of the (single) non-anchor side of a rated match, or
    None when the match does not participate."""
    if t.status != COMPLETED or not t.arena_elo_enabled:
        return None
    snap = t.config_snapshot or {}
    side_a, side_b = snap.get("engine_a") or {}, snap.get("engine_b") or {}
    if is_anchor(session, side_a) == is_anchor(session, side_b):
        return None
    engine_side = side_b if is_anchor(session, side_a) else side_a
    fingerprint = engine_fingerprint(engine_side)
    all_ratings = compute_ratings(session)
    for row in all_ratings.get(t.time_control, {}).get("engines", []):
        if row.get("fingerprint") == fingerprint:
            return row
    return None
