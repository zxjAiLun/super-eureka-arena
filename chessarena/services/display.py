"""Shared display helpers (P4.11 commit 4): one place for the site-wide
W-D-L / Δ Elo / friendly time-control vocabulary, so no template or page
can drift into its own formatting."""

from __future__ import annotations

import math
from typing import Optional

from ..config import TIME_CONTROLS

# Compact time-control labels (same semantics as the React Live page map):
#   bullet_1_0 -> 1+0   blitz_3_2 -> 3+2   rapid_5_3 -> 5+3
#   blitz_10_01 -> 10s+0.1s
TC_LABELS = {
    key: (val.get("friendly_label") or key)
    for key, val in TIME_CONTROLS.items()
}


def tc_label(key: str) -> str:
    """Friendly time-control label; never the internal config key."""
    return TC_LABELS.get(key) or key


def wdl_text(wins: int, draws: int, losses: int) -> str:
    """Candidate-perspective W-D-L, e.g. '185-73-142'."""
    return f"{wins}-{draws}-{losses}"


# Score -> performance Elo delta (Engine A relative to Engine B):
#
#   N = W + D + L
#   S = (W + 0.5D) / N
#   Δ = 400 * log10(S / (1 - S))
#
# This is a match-result derived performance delta, NOT a P4.8 Arena Elo
# mutation.  Extremes are clamped (no division by zero, no Infinity):
# 100% -> +800, 0% -> -800.  Returns None when nothing was played.
def match_elo_delta(wins: int, draws: int, losses: int) -> Optional[int]:
    played = wins + draws + losses
    if played <= 0:
        return None
    score = (wins + 0.5 * draws) / played
    if score >= 1.0:
        return 800
    if score <= 0.0:
        return -800
    delta = 400 * math.log10(score / (1 - score))
    return int(round(delta))


def elo_delta_text(delta: Optional[int]) -> str:
    """Signed integer text ('+38', '-24', '0'); '—' when unknown."""
    if delta is None:
        return "—"
    if delta > 0:
        return f"+{delta}"
    return str(delta)


def elo_delta_label(wins: int, draws: int, losses: int) -> str:
    """User-facing Δ Elo (A−B) with the extreme case rendered as a BOUND:
    100% score is +∞ mathematically, so it displays as '≥+800' (0% as
    '≤-800') — never as an exact '+800'/'−800' value."""
    played = wins + draws + losses
    if played <= 0:
        return "—"
    score = (wins + 0.5 * draws) / played
    if score >= 1.0:
        return "≥+800"
    if score <= 0.0:
        return "≤-800"
    return elo_delta_text(match_elo_delta(wins, draws, losses))
