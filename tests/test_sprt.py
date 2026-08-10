"""S4.3D pentanomial SPRT tests.

Includes the differential validation of the Arena implementation against the
official Fishtest ``LLRcalc.LLR_logistic`` (vendored under ``tests/reference``,
scipy required for tests only).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "reference"))
from fishtest_LLRcalc import LLR_logistic  # type: ignore

from chessarena.services import sprt


def approx_llr(ptnml, elo0=10.0, elo1=30.0):
    return sprt.pentanomial_llr(elo0, elo1, ptnml)


@pytest.mark.parametrize(
    "ptnml",
    [
        # all-neutral pentanomial (perfect balance)
        [25, 25, 50, 25, 25],
        # strong H1 (candidate dominates)
        [0, 0, 0, 0, 100],
        [5, 10, 20, 40, 25],
        # strong H0 (candidate dominated)
        [100, 0, 0, 0, 0],
        [40, 40, 20, 0, 0],
        # mixed
        [25, 20, 80, 37, 38],
        [10, 10, 40, 20, 20],
        # boundary-crossing examples
        [30, 30, 40, 60, 40],
        [60, 40, 60, 20, 20],
        # degenerate regularization-only cells
        [0, 0, 1, 0, 0],
    ],
)
def test_differential_against_fishtest_llrcalc(ptnml):
    ours = approx_llr(ptnml)
    reference = LLR_logistic(10.0, 30.0, list(ptnml))
    assert ours == pytest.approx(reference, abs=1e-6)


def test_all_neutral_is_balanced():
    # Perfectly balanced pentanomial under H0=+10/H1=+30: the generalized LLR
    # is small (well inside the boundaries) and exactly matches Fishtest (the
    # differential test already pins the value).
    llr = approx_llr([25, 25, 50, 25, 25])
    assert abs(llr) < 2.944438979
    assert llr == pytest.approx(LLR_logistic(10.0, 30.0, [25, 25, 50, 25, 25]), abs=1e-6)


def test_strong_h1_positive():
    assert approx_llr([0, 0, 0, 0, 100]) > 0
    assert approx_llr([5, 10, 20, 40, 25]) > 0


def test_strong_h0_negative():
    assert approx_llr([100, 0, 0, 0, 0]) < 0
    assert approx_llr([40, 40, 20, 0, 0]) < 0


def test_inversion_symmetry():
    # Inversion symmetry holds for symmetric hypotheses (elo0 = -elo1), where
    # the score scale is symmetric around 0.5.
    llr = sprt.pentanomial_llr(-20.0, 20.0, [25, 20, 80, 37, 38])
    flipped = sprt.pentanomial_llr(-20.0, 20.0, [38, 37, 80, 20, 25])
    assert llr == pytest.approx(-flipped, abs=1e-6)


def test_pair_points_index():
    assert sprt.pair_points_index(2, 0, 0) == 4  # WW
    assert sprt.pair_points_index(1, 0, 1) == 3  # W+D
    assert sprt.pair_points_index(1, 1, 0) == 2  # W+L
    assert sprt.pair_points_index(0, 0, 2) == 2  # D+D
    assert sprt.pair_points_index(0, 1, 1) == 1  # L+D
    assert sprt.pair_points_index(0, 2, 0) == 0  # LL
    with pytest.raises(ValueError):
        sprt.pair_points_index(2, 1, 0)  # impossible pair accounting


def test_wald_bounds():
    lower, upper = sprt.wald_bounds(0.05, 0.05)
    assert lower == pytest.approx(-2.944438979, abs=1e-6)
    assert upper == pytest.approx(2.944438979, abs=1e-6)


def test_sprt_decision_boundary_crossing():
    # Strong H1: LLR above upper -> ACCEPT_H1
    r = sprt.sprt_llr_and_decision(10, 30, 0.05, 0.05, [0, 0, 0, 0, 100], max_pairs=2000)
    assert r["decision"] == "ACCEPT_H1"
    assert r["llr"] >= r["upper_bound"]
    # Strong H0
    r = sprt.sprt_llr_and_decision(10, 30, 0.05, 0.05, [100, 0, 0, 0, 0], max_pairs=2000)
    assert r["decision"] == "ACCEPT_H0"
    assert r["llr"] <= r["lower_bound"]
    # Neutral small sample -> CONTINUE
    r = sprt.sprt_llr_and_decision(10, 30, 0.05, 0.05, [2, 2, 4, 2, 2], max_pairs=2000)
    assert r["decision"] == "CONTINUE"
    # Max pairs without boundary -> MAX_PAIRS
    r = sprt.sprt_llr_and_decision(10, 30, 0.05, 0.05, [2, 2, 4, 2, 2], max_pairs=12)
    assert r["decision"] == "MAX_PAIRS"


def test_regularization_matches_fishtest():
    # Fishtest regularizes zero cells with epsilon before computing.
    ours = approx_llr([0, 0, 200, 0, 0])
    reference = LLR_logistic(10.0, 30.0, [0, 0, 200, 0, 0])
    assert ours == pytest.approx(reference, abs=1e-6)
