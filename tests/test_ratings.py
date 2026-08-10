"""P4.12 rating v2 tests: standard participant Elo (K=16, per verified game,
per TC pool), fixed Stockfish anchors, engine-vs-engine both update, all
public participants shown even with zero games, deterministic recompute."""

from __future__ import annotations

import pytest

from chessarena.models import (
    COMPLETED,
    EngineBuild,
    Game,
    Tournament,
    utcnow,
)
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


def _result_for_a(a_white, a_score):
    """Result string (White/Black perspective) from Engine A's score."""
    if a_score == 1.0:
        return "1-0" if a_white else "0-1"
    if a_score == 0.5:
        return "1/2-1/2"
    return "0-1" if a_white else "1-0"


def _real_engine_side(session, engine=None):
    """A side whose fingerprint matches the registered public preset (or a
    custom side when `engine` is given, e.g. for engine-vs-engine)."""
    if engine is not None:
        return dict(engine)
    from chessarena.models import EnginePreset

    preset = (
        session.query(EnginePreset)
        .filter(EnginePreset.preset_id == "chessengine-production")
        .one()
    )
    build = (
        session.query(EngineBuild)
        .filter(EngineBuild.build_id == preset.build_id)
        .one()
    )
    return {
        "preset_id": preset.preset_id,
        "display_name": preset.display_name,
        "build_id": build.build_id,
        "command_args": list(preset.command_args or []),
        "uci_options": dict(preset.uci_options or {}),
        "binary_sha256": build.binary_sha256,
    }


def _completed_rated(engine_factory, tournament_factory, *, tc="blitz_3_2",
                     wins, losses, draws, anchor_a=True, anchor=SF_A,
                     pairs=None, enabled=True, engine=None,
                     verified=True):
    """Create a rated COMPLETED match with real per-game rows.  `wins` /
    `draws` / `losses` are the ENGINE's perspective totals; the first games
    are engine wins, then draws, then losses (deterministic path)."""
    if pairs is None:
        pairs = (wins + draws + losses) // 2
    tid = tournament_factory(
        name="rated", pairs=pairs, time_control=tc, status=COMPLETED
    )
    with engine_factory() as session:
        t = session.query(Tournament).filter(Tournament.id == tid).one()
        t.completed_pairs = pairs
        engine_side = _real_engine_side(session, engine)
        snap = dict(t.config_snapshot or {})
        if anchor_a:
            snap["engine_a"] = dict(anchor)
            snap["engine_b"] = dict(engine_side)
            engine_on_a = False
        else:
            snap["engine_a"] = dict(engine_side)
            snap["engine_b"] = dict(anchor)
            engine_on_a = True
        # Candidate (A-side) aggregates for the match summary.
        if engine_on_a:
            t.candidate_wins = wins
            t.candidate_losses = losses
        else:
            t.candidate_wins = losses
            t.candidate_losses = wins
        t.draws = draws
        t.finished_at = utcnow()
        t.arena_elo_enabled = enabled
        t.config_snapshot = snap
        pair = t.pair_jobs[0]
        pair.status = "COMPLETED"
        pair.return_code = 0
        seq = [1.0] * wins + [0.5] * draws + [0.0] * losses
        for i, engine_score in enumerate(seq):
            game_number = i + 1
            a_white = game_number % 2 == 1
            a_score = engine_score if engine_on_a else 1 - engine_score
            g = Game(
                tournament_id=tid,
                pair_job_id=pair.id,
                game_number=game_number,
                white_engine="EngineA" if a_white else "EngineB",
                black_engine="EngineB" if a_white else "EngineA",
                opening_index=0,
                result=_result_for_a(a_white, a_score),
                pgn_path="/unused/rated.pgn",
                verified=verified,
            )
            session.add(g)
        session.commit()
    return tid


def _engine_row(session, pool="blitz_3_2", display="ChessEngine Production"):
    for row in ratings.compute_ratings(session)[pool]["engines"]:
        if row["display_name"] == display:
            return row
    return None


def test_zero_game_participants_show_initial(app_client, engine_factory,
                                             registered):
    """All public/enabled participants appear even with no rated history."""
    with engine_factory() as session:
        rows = ratings.compute_ratings(session)["blitz_3_2"]["engines"]
    names = {r["display_name"]: r for r in rows}
    assert "ChessEngine Production" in names
    assert "ChessEngine Legacy Baseline" in names
    for r in rows:
        assert r["status"] in ("initial", "fixed")
        if r["status"] == "initial":
            assert r["rating"] == 1800
            assert r["games"] == 0
            assert (r["wins"], r["draws"], r["losses"]) == (0, 0, 0)


def test_fifty_percent_vs_anchor_moves_toward_anchor(engine_factory,
                                                     tournament_factory):
    """10 games at 50% vs a 2000 anchor: the engine climbs from 1800 toward
    2000 but stays below; the anchor stays fixed at 2000."""
    _completed_rated(engine_factory, tournament_factory,
                     wins=5, losses=5, draws=0, anchor=SF_A)
    with engine_factory() as session:
        row = _engine_row(session)
        anchors = ratings.compute_ratings(session)["blitz_3_2"]["anchors"]
    assert 1800 < row["rating"] < 2000, row["rating"]
    assert row["status"] == "rated"
    assert row["games"] == 10
    assert (row["wins"], row["draws"], row["losses"]) == (5, 0, 5)
    assert anchors == [{"rating": 2000, "display_name": "Stockfish Limited 2000"}]


def test_all_wins_vs_anchor_above_anchor(engine_factory, tournament_factory):
    """20 straight wins vs SF2000 (K=16, E=0.2403): rating exceeds 2000."""
    _completed_rated(engine_factory, tournament_factory,
                     wins=20, losses=0, draws=0, anchor=SF_A)
    with engine_factory() as session:
        row = _engine_row(session)
    assert row["rating"] > 2000, row["rating"]


def test_anchor_never_updates(engine_factory, tournament_factory):
    _completed_rated(engine_factory, tournament_factory,
                     wins=10, losses=0, draws=0, anchor=SF_A)
    _completed_rated(engine_factory, tournament_factory,
                     wins=0, losses=10, draws=0, anchor=SF_A)
    with engine_factory() as session:
        pools = ratings.compute_ratings(session)
    for r in pools["blitz_3_2"]["engines"]:
        if r["display_name"] == "Stockfish Limited 2000":
            assert r["rating"] == 2000
            assert r["status"] == "fixed"


def test_engine_vs_engine_both_update(engine_factory, tournament_factory):
    """Engine-vs-engine: the winner climbs, the loser drops, both leave 1800."""
    other = dict(ENGINE)
    other["display_name"] = "Second Engine"
    other["binary_sha256"] = "other-sha"
    _completed_rated(engine_factory, tournament_factory,
                     wins=6, losses=2, draws=2, anchor_a=False,
                     anchor=other, engine=ENGINE)
    with engine_factory() as session:
        rows = ratings.compute_ratings(session)["blitz_3_2"]["engines"]
    by_name = {r["display_name"]: r for r in rows}
    assert by_name["ChessEngine Production"]["rating"] > 1800
    assert by_name["Second Engine"]["rating"] < 1800
    assert by_name["ChessEngine Production"]["rating"] > by_name["Second Engine"]["rating"]


def test_perspective_not_reversed(engine_factory, tournament_factory):
    """Engine on A or B must produce the same rating for the same results
    (each case in its own pool so the two matches never interact)."""
    _completed_rated(engine_factory, tournament_factory,
                     wins=4, losses=2, draws=0, anchor_a=False, anchor=SF_A)
    with engine_factory() as session:
        first = _engine_row(session)
    _completed_rated(engine_factory, tournament_factory,
                     wins=4, losses=2, draws=0, anchor_a=True, anchor=SF_A,
                     tc="bullet_1_0")
    with engine_factory() as session:
        second = _engine_row(session, pool="bullet_1_0")
    assert first is not None and second is not None
    assert first["rating"] == second["rating"], (first, second)
    assert first["wins"] == second["wins"] == 4
    assert first["losses"] == second["losses"] == 2


def test_deterministic_recompute(engine_factory, tournament_factory):
    _completed_rated(engine_factory, tournament_factory,
                     wins=3, losses=7, draws=0, anchor=SF_A)
    _completed_rated(engine_factory, tournament_factory,
                     wins=8, losses=2, draws=0, anchor=SF_B)
    with engine_factory() as session:
        first = ratings.compute_ratings(session)["blitz_3_2"]["engines"]
        second = ratings.compute_ratings(session)["blitz_3_2"]["engines"]
    assert first == second


def test_tc_pools_do_not_mix(engine_factory, tournament_factory):
    _completed_rated(engine_factory, tournament_factory,
                     wins=5, losses=5, draws=0, anchor=SF_A, tc="bullet_1_0")
    _completed_rated(engine_factory, tournament_factory,
                     wins=5, losses=5, draws=0, anchor=SF_B, tc="rapid_5_3")
    with engine_factory() as session:
        pools = ratings.compute_ratings(session)
    bullet = _engine_row(session, pool="bullet_1_0")
    rapid = _engine_row(session, pool="rapid_5_3")
    assert bullet["games"] == 10 and rapid["games"] == 10
    # 5W/5L vs a 2000 anchor climbs well above 1800; 5W/5L vs an equal 1800
    # anchor stays near 1800 (the exact value depends on the fixed-anchor
    # drift path — the pools must never mix).
    assert bullet["rating"] > 1800
    assert abs(rapid["rating"] - 1800) <= 10, rapid["rating"]
    assert bullet["rating"] > rapid["rating"]


def test_unverified_games_do_not_count(engine_factory, tournament_factory):
    _completed_rated(engine_factory, tournament_factory,
                     wins=10, losses=0, draws=0, anchor=SF_A,
                     verified=False)
    with engine_factory() as session:
        row = _engine_row(session)
    assert row is None or row["games"] == 0
    if row is not None:
        assert row["status"] == "initial"


def test_delete_changes_recomputation(engine_factory, tournament_factory,
                                      settings, app_client):
    tid = _completed_rated(engine_factory, tournament_factory,
                           wins=10, losses=0, draws=0, anchor=SF_A)
    with engine_factory() as session:
        before = _engine_row(session)
    assert before["rating"] > 1800
    app_client.get("/chessarena/admin/tournaments/new")
    token = app_client.cookies.get("arena_csrf")
    r = app_client.post(
        f"/chessarena/admin/tournaments/{tid}/delete",
        data={"_csrf_token": token},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text[:200]
    with engine_factory() as session:
        after = _engine_row(session)
    assert after["games"] == 0
    assert after["rating"] == 1800
    assert after["status"] == "initial"


def test_custom_elo_anchor_recognized(engine_factory, tournament_factory):
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
    row = _engine_row(session)
    assert 1800 < row["rating"] < 1850


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
                     wins=5, losses=5, draws=0, anchor=ghost)
    with engine_factory() as session:
        pools = ratings.compute_ratings(session)
    # The ghost side is a normal (non-anchor) participant: it must NOT appear
    # in anchors, and it is not an "initial" 1800-untouched participant only
    # if it has games — it does, so it is rated (never fixed).
    anchors = pools["blitz_3_2"]["anchors"]
    assert anchors == []
    for r in pools["blitz_3_2"]["engines"]:
        if r["display_name"] == "Stockfish Limited 2000":
            assert r["status"] != "fixed"
            assert r["rating"] != 2000


def test_engine_rating_helper(engine_factory, tournament_factory):
    tid = _completed_rated(engine_factory, tournament_factory,
                           wins=6, losses=4, draws=0, anchor=SF_A)
    with engine_factory() as session:
        t = session.query(Tournament).filter(Tournament.id == tid).one()
        row = ratings.engine_rating(session, t)
    assert row is not None
    assert row["display_name"] == "ChessEngine Production"
    assert row["games"] == 10
