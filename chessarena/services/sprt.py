"""Formal pentanomial SPRT (S4.3D).

Self-contained implementation of the sequential probability ratio test on the
pentanomial distribution under the logistic Elo model, mirroring the official
Stockfish/Fishtest formulation (``LLRcalc.LLR_logistic`` / ``sprt.py``):

- Elo -> expected score:  ``s(elo) = 1 / (1 + 10^(-elo/400))``
- The five pair outcomes (candidate points 0.0 .. 2.0 out of 2.0) follow the
  Fishtest ordering ``[LL, LD+DL, LW+DD+WL, DW+WD, WW]``.
- Zero-count cells are regularized with ``epsilon = 1e-3`` exactly like
  Fishtest ``regularize``.
- The generalized log-likelihood ratio uses the MLE of the multinomial with a
  fixed expectation (Fishtest ``MLE_expected``); the secular equation is solved
  by bisection instead of scipy (no runtime dependency).

Differential-tested against the official ``fishtest/stats/LLRcalc.py`` in
``tests/test_sprt.py`` on fixed pentanomial vectors (see ``tests/reference/``).
"""

from __future__ import annotations

import math
from typing import Sequence

# Ptnml cell ordering (candidate points out of 2.0):
#   index 0 = 0.0/2.0  (LL)
#   index 1 = 0.5/2.0  (LD + DL)
#   index 2 = 1.0/2.0  (LW + DD + WL)
#   index 3 = 1.5/2.0  (DW + WD)
#   index 4 = 2.0/2.0  (WW)
PTNML_SIZE = 5

REGULARIZE_EPSILON = 1e-3
SECULAR_EPSILON = 1e-9
MLE_TOLERANCE = 1e-6


def logistic_elo_to_score(elo: float) -> float:
    """Logistic Elo -> expected score (Fishtest ``L_``)."""
    return 1.0 / (1.0 + 10.0 ** (-elo / 400.0))


def wald_bounds(alpha: float, beta: float) -> tuple[float, float]:
    """Wald SPRT boundaries ``[lower, upper]``."""
    lower = math.log(beta / (1.0 - alpha))
    upper = math.log((1.0 - beta) / alpha)
    return lower, upper


def pair_points_index(candidate_wins: int, candidate_losses: int, candidate_draws: int) -> int:
    """Classify ONE verified pair into a Ptnml index (candidate points in
    half-points: ``2*wins + draws`` -> 0..4)."""
    if candidate_wins + candidate_losses + candidate_draws != 2:
        raise ValueError(
            f"pair must contain exactly 2 games, got w={candidate_wins} "
            f"l={candidate_losses} d={candidate_draws}"
        )
    pts = 2 * candidate_wins + candidate_draws
    if not 0 <= pts <= 4:
        raise ValueError(f"invalid pair score {pts} (w={candidate_wins} "
                         f"l={candidate_losses} d={candidate_draws})")
    return pts


def _regularize(ptnml: Sequence[int]) -> list[float]:
    if len(ptnml) != PTNML_SIZE:
        raise ValueError(f"pentanomial needs {PTNML_SIZE} cells, got {len(ptnml)}")
    return [REGULARIZE_EPSILON if v == 0 else float(v) for v in ptnml]


def _results_to_pdf(ptnml: Sequence[int]) -> tuple[float, list[tuple[float, float]]]:
    """Fishtest ``results_to_pdf``: pair score values ``i/4`` with empirical
    probabilities, zero cells regularized."""
    reg = _regularize(ptnml)
    total = sum(reg)
    return total, [(i / (PTNML_SIZE - 1), reg[i] / total) for i in range(PTNML_SIZE)]


def _secular_root(values: Sequence[float], probs: Sequence[float]) -> float:
    """Solve ``sum_i p_i * a_i / (1 + x * a_i) = 0`` by bisection. Requires
    the support of ``values`` to straddle zero (guaranteed after
    regularization)."""
    v = min(values)
    w = max(values)
    if v * w >= 0:
        raise ValueError("secular equation requires support straddling zero")
    lower = -1.0 / w + SECULAR_EPSILON
    upper = -1.0 / v - SECULAR_EPSILON

    def f(x: float) -> float:
        return sum(p * a / (1.0 + x * a) for a, p in zip(values, probs))

    # f is strictly decreasing on (lower, upper); f(lower) -> +inf, f(upper) -> -inf.
    lo, hi = lower, upper
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if f(mid) > 0:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-12:
            break
    return 0.5 * (lo + hi)


def _mle_expected(pdf: Sequence[tuple[float, float]], s: float) -> list[tuple[float, float]]:
    """Fishtest ``MLE_expected``: MLE of the multinomial with fixed
    expectation ``s``, using the exponential tilt family."""
    shifted = [(a - s, p) for a, p in pdf]
    x = _secular_root([a for a, _ in shifted], [p for _, p in shifted])
    mle = [(a, p / (1.0 + x * (a - s))) for a, p in pdf]
    mean = sum(p * a for a, p in mle)
    if abs(mean - s) > MLE_TOLERANCE:
        raise ValueError(f"MLE_expected did not converge to s={s} (got {mean})")
    return mle


def _llr(pdf: Sequence[tuple[float, float]], s0: float, s1: float) -> float:
    """Fishtest ``LLR`` (statistic="expectation"): generalized log-likelihood
    ratio per observation for s1 vs s0."""
    pdf0 = _mle_expected(pdf, s0)
    pdf1 = _mle_expected(pdf, s1)
    return sum(p * (math.log(p1) - math.log(p0))
               for (_, p0), (_, p1), (_, p) in zip(pdf0, pdf1, pdf))


def pentanomial_llr(elo0: float, elo1: float, ptnml: Sequence[int]) -> float:
    """Log-likelihood ratio (logistic model) for the pentanomial counts
    ``[n0..n4]`` (candidate points 0.0..2.0). Mirrors Fishtest
    ``LLR_logistic(elo0, elo1, results)``."""
    s0 = logistic_elo_to_score(elo0)
    s1 = logistic_elo_to_score(elo1)
    n, pdf = _results_to_pdf(ptnml)
    return n * _llr(pdf, s0, s1)


def sprt_llr_and_decision(
    elo0: float,
    elo1: float,
    alpha: float,
    beta: float,
    ptnml: Sequence[int],
    max_pairs: int,
) -> dict:
    """Compute LLR and the sequential decision from verified pairs only."""
    pairs = sum(ptnml)
    llr = pentanomial_llr(elo0, elo1, ptnml)
    lower, upper = wald_bounds(alpha, beta)
    if llr >= upper:
        decision = "ACCEPT_H1"
    elif llr <= lower:
        decision = "ACCEPT_H0"
    elif pairs >= max_pairs:
        decision = "MAX_PAIRS"
    else:
        decision = "CONTINUE"
    return {
        "pairs": pairs,
        "games": 2 * pairs,
        "ptnml": list(ptnml),
        "llr": llr,
        "lower_bound": lower,
        "upper_bound": upper,
        "decision": decision,
    }


# ---------------------------------------------------------------------------
# V2.2-A: shared SPRT read model (single source for worker + UI)
# ---------------------------------------------------------------------------
def pentanomial_from_pairs(pair_jobs) -> list[int]:
    """Pentanomial counts over the VERIFIED COMPLETED pair jobs.

    The single implementation the scheduler's SPRT check and the admin UI
    both use — the two can never drift. Pair classification goes through
    ``pair_points_index`` on each pair's frozen
    ``verification.candidate_perspective``.
    """
    from ..models import COMPLETED

    ptnml = [0] * PTNML_SIZE
    for pair_job in pair_jobs:
        if pair_job.status != COMPLETED:
            continue
        verification = pair_job.verification or {}
        computed = verification.get("candidate_perspective") or {}
        ptnml[pair_points_index(
            int(computed.get("wins", 0)),
            int(computed.get("losses", 0)),
            int(computed.get("draws", 0)),
        )] += 1
    return ptnml


def tournament_sprt_state(tournament) -> dict | None:
    """The live SPRT state of a tournament, or None when its frozen
    snapshot carries no enabled SPRT contract.

    Wraps the one math implementation (``sprt_llr_and_decision``) with the
    frozen contract parameters, so the worker decision, the admin UI and
    the artifact evidence are all computed by the same code path.
    """
    snap = tournament.config_snapshot or {}
    cfg = snap.get("sprt")
    if not cfg or not cfg.get("enabled"):
        return None
    ptnml = pentanomial_from_pairs(tournament.pair_jobs)
    result = sprt_llr_and_decision(
        elo0=float(cfg["elo0"]),
        elo1=float(cfg["elo1"]),
        alpha=float(cfg["alpha"]),
        beta=float(cfg["beta"]),
        ptnml=ptnml,
        max_pairs=int(cfg["max_pairs"]),
    )
    return {
        **result,
        "elo0": cfg.get("elo0"),
        "elo1": cfg.get("elo1"),
        "alpha": cfg.get("alpha"),
        "beta": cfg.get("beta"),
        "max_pairs": cfg.get("max_pairs"),
    }
