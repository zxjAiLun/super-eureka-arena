"""P4.8 Arena Elo tests: anchor-calibrated ratings recomputed from rated
history, per time-control pools, with a frozen-config identity."""

from __future__ import annotations

import math

import pytest

from chessarena.models import COMPLETED, EngineBuild, Tournament, utcnow
from chessarena.services import ratings

SF_A = {
    "preset_id": "stockfish-limited-2000",
    "display_name": "Stockfish Limited 2000",
    "build_id": "stockfish-build",
    "uci_options": {"UCI_LimitStrength": True, "UCI_Elo": 2000},
}
SF_B = {
    "preset_id": "stockfish-limited-1800",
    "display_name": "Stockfish Limited 1800",
    "build_id": "stockfish-build",
    "uci_options": {"UCI_LimitStrength": True, "UCI_Elo": 1800},
}
ENGINE = {
    "preset_id": "chessengine-production",
    "display_name": "ChessEngine Production",
    "build_id": "engine-build",
    "command_args": ["--profile", "current-final"],
    "uci_options": {},
    "binary_sha256": "engine-sha",
}


@pytest.fixture(autouse=True)
def stockfish_anchor_build(engine_factory):
    """The anchor recognition is fail-closed: the Stockfish build row must
    actually exist for a snapshot side to be treated as a fixed anchor."""
    with engine_factory() as session:
        session.add(
            EngineBuild(
                build_id="stockfish-build",
                engine_name="Stockfish",
                git_sha="external",
                binary_path="/unused/stockfish",
                binary_sha256="a" * 64,
                platform="linux-x86_64",
                supported_profiles=[],
                manifest={},
                enabled=True,
            )
        )
        session.commit()


def _completed_rated(engine_factory, tournament_factory, *, tc="blitz_3_2",
                     wins, losses, draws, anchor_a=True, anchor=SF_A,
                     pairs=None, enabled=True):
    """Create a rated COMPLETED match.  Since P4.11 commit 4 the game count
    is the ACTUAL played games (W+D+L), so callers must pass pairs matching
    the result totals (pairs * 2 == wins + draws + losses) unless they set
    pairs explicitly for an early-stop scenario."""
    if pairs is None:
        pairs = (wins + draws + losses) // 2
    tid = tournament_factory(
        name="rated", pairs=pairs, time_control=tc, status=COMPLETED
    )
    with engine_factory() as session:
        t = session.query(Tournament).filter(Tournament.id == tid).one()
        t.completed_pairs = pairs
        t.candidate_wins = wins
        t.candidate_losses = losses
        t.draws = draws
        t.finished_at = utcnow()
        t.arena_elo_enabled = enabled
        # Assign a fresh dict so SQLAlchemy tracks the JSON change.
        snap = dict(t.config_snapshot or {})
        if anchor_a:
            snap["engine_a"] = dict(anchor)
            snap["engine_b"] = dict(ENGINE)
        else:
            snap["engine_a"] = dict(ENGINE)
            snap["engine_b"] = dict(anchor)
        t.config_snapshot = snap
        session.commit()
    return tid


def test_single_anchor_50pct_equals_anchor(engine_factory, tournament_factory):
    # Engine on side B: engine score = candidate_losses + 0.5*draws = 5/10.
    _completed_rated(engine_factory, tournament_factory,
                     wins=5, losses=5, draws=0, anchor=SF_A)
    with engine_factory() as session:
        pools = ratings.compute_ratings(session)
    rows = pools["blitz_3_2"]["engines"]
    assert len(rows) == 1
    assert rows[0]["rating"] == 2000
    assert rows[0]["games"] == 10  # actual played games (W+D+L)


def test_known_logistic_score_above_anchor(engine_factory, tournament_factory):
    # 75% vs a 2000 anchor -> expected E = 0.75 at R where 1/(1+10^((2000-R)/400)) = 0.75
    # -> R = 2000 + 400 * log10(3) ~= 2190.8
    _completed_rated(engine_factory, tournament_factory,
                     wins=5, losses=15, draws=0, anchor=SF_A)
    with engine_factory() as session:
        rows = ratings.compute_ratings(session)["blitz_3_2"]["engines"]
    expected = 2000 + 400 * math.log10(3)
    assert abs(rows[0]["rating"] - expected) < 1


def test_multi_anchor_deterministic_order_independent(
    engine_factory, tournament_factory
):
    _completed_rated(engine_factory, tournament_factory,
                     wins=8, losses=12, draws=0, anchor=SF_A)  # 60% vs 2000
    _completed_rated(engine_factory, tournament_factory,
                     wins=4, losses=16, draws=0, anchor=SF_B)  # 80% vs 1800
    _completed_rated(engine_factory, tournament_factory,
                     wins=16, losses=4, draws=0, anchor=SF_A)  # 20% vs 2000
    with engine_factory() as session:
        first = ratings.compute_ratings(session)["blitz_3_2"]["engines"][0]["rating"]
    # Same data, different creation order must not change the result.
    with engine_factory() as session:
        rows = (
            session.query(Tournament)
            .filter(Tournament.status == COMPLETED)
            .order_by(Tournament.name)
            .all()
        )
        for r in rows:
            r.finished_at = utcnow()
        session.commit()
        second = ratings.compute_ratings(session)["blitz_3_2"]["engines"][0]["rating"]
    assert first == second


def test_unrated_or_incomplete_excluded(engine_factory, tournament_factory):
    _completed_rated(engine_factory, tournament_factory,
                     wins=0, losses=20, draws=0, anchor=SF_A, enabled=False)
    _completed_rated(engine_factory, tournament_factory,
                     wins=10, losses=10, draws=0, anchor=SF_B)  # 50% vs 1800
    tid = tournament_factory(name="draft", pairs=2, status="DRAFT")
    with engine_factory() as session:
        t = session.query(Tournament).filter(Tournament.id == tid).one()
        t.arena_elo_enabled = True
        t.config_snapshot = {"engine_a": dict(SF_A), "engine_b": dict(ENGINE)}
        session.commit()
    with engine_factory() as session:
        rows = ratings.compute_ratings(session)["blitz_3_2"]["engines"]
    # Only the rated, completed match vs SF_B participates.
    assert len(rows) == 1
    assert rows[0]["rating"] == 1800


def test_delete_changes_recomputation(engine_factory, tournament_factory,
                                      settings, app_client):
    tid = _completed_rated(engine_factory, tournament_factory,
                           wins=0, losses=20, draws=0, anchor=SF_B)
    with engine_factory() as session:
        before = ratings.compute_ratings(session)["blitz_3_2"]["engines"]
    assert len(before) == 1
    app_client.get("/chessarena/admin/tournaments/new")
    token = app_client.cookies.get("arena_csrf")
    r = app_client.post(
        f"/chessarena/admin/tournaments/{tid}/delete",
        data={"_csrf_token": token},
        follow_redirects=False,
    )
    assert r.status_code == 303
    with engine_factory() as session:
        after = ratings.compute_ratings(session)["blitz_3_2"]["engines"]
    assert after == []


def test_custom_elo_anchor_recognized(engine_factory, tournament_factory):
    # A custom 1850 override freezes uci_options -> recognized as 1850 anchor.
    custom = {
        "preset_id": "stockfish-limited-2000",
        "display_name": "Stockfish Limited 1850",
        "build_id": "stockfish-build",
        "uci_options": {"UCI_LimitStrength": True, "UCI_Elo": 1850},
    }
    _completed_rated(engine_factory, tournament_factory,
                     wins=5, losses=5, draws=0, anchor=custom)
    with engine_factory() as session:
        pools = ratings.compute_ratings(session)
    assert pools["blitz_3_2"]["anchors"] == [
        {"rating": 1850, "display_name": "Stockfish Limited 1850"}
    ]
    assert pools["blitz_3_2"]["engines"][0]["rating"] == 1850


def test_tc_pools_do_not_mix(engine_factory, tournament_factory):
    _completed_rated(engine_factory, tournament_factory,
                     wins=10, losses=10, draws=0, anchor=SF_B, tc="bullet_1_0")
    _completed_rated(engine_factory, tournament_factory,
                     wins=5, losses=5, draws=0, anchor=SF_A, tc="rapid_5_3")
    with engine_factory() as session:
        pools = ratings.compute_ratings(session)
    assert pools["bullet_1_0"]["engines"][0]["rating"] == 1800
    assert pools["rapid_5_3"]["engines"][0]["rating"] == 2000
    assert pools["blitz_3_2"]["engines"] == []


def test_engine_on_anchor_side_scored_from_engine_perspective(
    engine_factory, tournament_factory
):
    # Engine on side A (anchor on B) wins 2/20 vs SF 2000 -> 10% -> rating < 2000.
    _completed_rated(engine_factory, tournament_factory,
                     wins=2, losses=18, draws=0, anchor_a=False, anchor=SF_A)
    with engine_factory() as session:
        rows = ratings.compute_ratings(session)["blitz_3_2"]["engines"]
    expected = 2000 - 400 * math.log10(9)
    assert abs(rows[0]["rating"] - expected) < 1


def test_missing_build_is_not_anchor(engine_factory, tournament_factory):
    """Fail closed: a snapshot side whose Stockfish build row is gone must not
    be treated as a fixed anchor (would corrupt the rating scale)."""
    ghost = {
        "preset_id": "stockfish-limited-2000",
        "display_name": "Stockfish Limited 2000",
        "build_id": "ghost-build",  # no EngineBuild row exists for this id
        "uci_options": {"UCI_LimitStrength": True, "UCI_Elo": 2000},
    }
    _completed_rated(engine_factory, tournament_factory,
                     wins=0, losses=10, draws=0, anchor=ghost)
    with engine_factory() as session:
        pools = ratings.compute_ratings(session)
    # No anchor and no engine rating: the match is engine-vs-"unknown" and
    # skipped entirely.
    assert pools["blitz_3_2"]["engines"] == []
    assert pools["blitz_3_2"]["anchors"] == []
