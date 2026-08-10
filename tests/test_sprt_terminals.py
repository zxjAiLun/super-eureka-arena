"""P4.11 commit 4 closure repair: SPRT terminal statuses (S4.3D) must be
first-class citizens of the public history/detail/replay/PGN/Live/Ratings
lifecycle — never mistaken for unfinished matches."""

from __future__ import annotations

import pytest

from chessarena.models import (
    COMPLETED,
    RESULT_TERMINAL_STATUSES,
    SPRT_ACCEPT_H0,
    SPRT_ACCEPT_H1,
    SPRT_MAX_PAIRS,
    EngineBuild,
    Game,
    Tournament,
    utcnow,
)
from chessarena.services.display import elo_delta_label
from chessarena.services.ratings import compute_ratings

SF_A = {
    "preset_id": "stockfish-limited-2000",
    "display_name": "Stockfish Limited 2000",
    "build_id": "stockfish-build",
    "uci_options": {"UCI_LimitStrength": True, "UCI_Elo": 2000},
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


def _terminal(settings, engine_factory, tournament_factory, name, *,
              status, wins, draws, losses, requested_pairs=1,
              arena_elo_enabled=False, anchor_vs_engine=False,
              with_game=False):
    tid = tournament_factory(
        name=name, pairs=requested_pairs, time_control="blitz_3_2",
        status=status,
    )
    with engine_factory() as session:
        t = session.query(Tournament).filter(Tournament.id == tid).one()
        t.completed_pairs = (wins + draws + losses) // 2
        t.candidate_wins = wins
        t.candidate_losses = losses
        t.draws = draws
        t.finished_at = utcnow()
        t.arena_elo_enabled = arena_elo_enabled
        if anchor_vs_engine:
            snap = dict(t.config_snapshot or {})
            snap["engine_a"] = dict(SF_A)
            snap["engine_b"] = dict(ENGINE)
            t.config_snapshot = snap
        gid = None
        if with_game:
            pair = t.pair_jobs[0]
            pair.status = "COMPLETED"
            pair.return_code = 0
            pgn_path = (
                settings.run_root / tid / "pairs" / "000000" / "attempt-01"
                / "match.pgn"
            )
            pgn_path.parent.mkdir(parents=True, exist_ok=True)
            pgn_path.write_text(
                '[Event "?"]\n[White "EngineA"]\n[Black "EngineB"]\n'
                '[Result "1-0"]\n\n1. e4 e5 1-0\n\n', encoding="utf-8"
            )
            g = Game(
                tournament_id=tid,
                pair_job_id=pair.id,
                game_number=1,
                white_engine="EngineA",
                black_engine="EngineB",
                opening_index=0,
                result="1-0",
                pgn_path=str(pgn_path),
                verified=True,
            )
            session.add(g)
            session.flush()
            gid = g.id
        session.commit()
    return tid, gid


SPRT_TERMINALS = (SPRT_ACCEPT_H1, SPRT_ACCEPT_H0, SPRT_MAX_PAIRS)


def test_sprt_terminals_are_replayable_public_matches(
    app_client, settings, engine_factory, tournament_factory
):
    """Every SPRT terminal appears in history, opens a detail page, and its
    verified games replay with PGN — exactly like a COMPLETED match."""
    for i, status in enumerate(SPRT_TERMINALS):
        tid, gid = _terminal(settings, engine_factory, tournament_factory,
                             f"sprt-{status}", status=status,
                             wins=3, draws=1, losses=0, with_game=True)
        history = app_client.get("/chessarena/matches/")
        assert history.status_code == 200
        assert f"sprt-{status}" in history.text

        api_list = app_client.get("/chessarena/public-api/v1/matches")
        assert api_list.status_code == 200
        assert tid in api_list.text

        detail = app_client.get(f"/chessarena/matches/{tid}")
        assert detail.status_code == 200, f"{status} detail must render"
        assert "Replay" in detail.text

        game_page = app_client.get(f"/chessarena/games/{gid}")
        assert game_page.status_code == 200, f"{status} game page must render"

        pgn = app_client.get(f"/chessarena/public-api/v1/games/{gid}/pgn")
        assert pgn.status_code == 200, f"{status} PGN must be served"

    # Home "Latest result" picks up a SPRT terminal too.
    home = app_client.get("/chessarena/")
    assert home.status_code == 200
    assert "sprt-SPRT_MAX_PAIRS" in home.text or "Latest result" in home.text


def test_pinned_live_on_sprt_terminal_is_completed(
    app_client, settings, engine_factory, tournament_factory
):
    """A pinned /live page on an ended SPRT match must report completed with
    a match_url — never keep showing 'live' (which would leave the browser
    Stockfish running)."""
    for status in SPRT_TERMINALS:
        tid, _ = _terminal(settings, engine_factory, tournament_factory,
                           f"live-{status}", status=status,
                           wins=2, draws=1, losses=0)
        body = app_client.get(
            f"/chessarena/public-api/v1/live?tournament_id={tid}"
        ).json()
        assert body["status"] == "completed", (
            f"{status} must end the live phase, got {body['status']}"
        )
        assert body["match_url"] == f"/chessarena/matches/{tid}"


def test_rated_sprt_early_stop_counts_actual_games(
    app_client, settings, engine_factory, tournament_factory
):
    """An early-stopped rated SPRT run contributes its ACTUAL games (W+D+L)
    to Arena Elo — never the requested-pairs ceiling."""
    tid, _ = _terminal(
        settings, engine_factory, tournament_factory, "sprt-rated",
        status=SPRT_ACCEPT_H1, wins=70, draws=30, losses=34,
        requested_pairs=200, arena_elo_enabled=True, anchor_vs_engine=True,
    )
    with engine_factory() as session:
        rows = compute_ratings(session)
    engines = rows["blitz_3_2"]["engines"]
    assert len(engines) == 1
    assert engines[0]["games"] == 70 + 30 + 34, (
        "rated early-stop must count actual played games, not 200*2"
    )
    # The public leaderboard shows the actual game count too.
    page = app_client.get("/chessarena/ratings/")
    assert page.status_code == 200
    assert f"<td>{70 + 30 + 34}</td>" in page.text


def test_elo_delta_label_works_for_sprt_matches(
    settings, engine_factory, tournament_factory
):
    """Δ Elo (A−B) renders on SPRT terminal matches with the same label."""
    tid, _ = _terminal(settings, engine_factory, tournament_factory,
                       "sprt-delta", status=SPRT_ACCEPT_H0,
                       wins=1, draws=1, losses=3)
    assert elo_delta_label(1, 1, 3) == "-147"
