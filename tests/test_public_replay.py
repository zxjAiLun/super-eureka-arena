"""Public replay tests (P4.1): anonymous read-only pages + API whitelist.

Covers:
- anonymous HTML pages (home, matches, match detail, game replay) return 200;
- public API lists only COMPLETED tournaments and verified games;
- whitelist: no build ids / SHAs / server paths leak;
- single-game PGN endpoint returns the matched game only;
- invalid ids and unverified games are 404.
"""

from __future__ import annotations

import json

import chess
import chess.pgn
import pytest

from chessarena.models import (
    COMPLETED,
    DRAFT,
    RUNNING,
    Game,
    Tournament,
    utcnow,
)

TEST_PGN_MOVES = [
    ("EngineA", "EngineB", "1-0", "1. e4 e5 2. Nf3 Nc6 3. Bb5 a6"),
    ("EngineB", "EngineA", "0-1", "1. e4 e5 2. Nf3 Nc6 3. Bb5 a6"),
]


def _write_match_pgn(path):
    """Write a two-game match.pgn (strict color swap) for one pair."""
    path.parent.mkdir(parents=True, exist_ok=True)
    texts = []
    for white, black, result, moves in TEST_PGN_MOVES:
        game = chess.pgn.Game()
        game.headers["Event"] = "?"
        game.headers["Site"] = "?"
        game.headers["Date"] = "2026.08.06"
        game.headers["Round"] = "1"
        game.headers["White"] = white
        game.headers["Black"] = black
        game.headers["Result"] = result
        node = game
        board = chess.Board()
        for token in moves.split():
            if token.endswith("."):
                continue
            move = board.parse_san(token)
            node = node.add_main_variation(move)
            board.push(move)
        texts.append(
            game.accept(
                chess.pgn.StringExporter(
                    headers=True, variations=False, comments=False
                )
            )
        )
    path.write_text("\n".join(texts) + "\n", encoding="utf-8")


@pytest.fixture()
def completed_match(settings, engine_factory, registered, tournament_factory):
    """A COMPLETED tournament with one verified pair (2 games + match.pgn)."""
    tid = tournament_factory(
        name="Public Match",
        pairs=1,
        time_control="blitz_3_2",
        status=COMPLETED,
    )
    with engine_factory() as session:
        t = session.query(Tournament).filter(Tournament.id == tid).one()
        t.completed_pairs = 1
        t.candidate_wins = 1
        t.candidate_losses = 0
        t.draws = 1
        t.finished_at = utcnow()
        pair = t.pair_jobs[0]
        pair.status = "COMPLETED"
        pair.return_code = 0
        pgn_path = (
            settings.run_root / tid / "pairs" / "000000" / "attempt-01" / "match.pgn"
        )
        _write_match_pgn(pgn_path)
        g1 = Game(
            tournament_id=tid,
            pair_job_id=pair.id,
            game_number=1,
            white_engine="EngineA",
            black_engine="EngineB",
            opening_index=0,
            result="1-0",
            termination=None,
            pgn_path=str(pgn_path),
            verified=True,
        )
        g2 = Game(
            tournament_id=tid,
            pair_job_id=pair.id,
            game_number=2,
            white_engine="EngineB",
            black_engine="EngineA",
            opening_index=0,
            result="0-1",
            termination=None,
            pgn_path=str(pgn_path),
            verified=True,
        )
        session.add_all([g1, g2])
        session.commit()
        session.refresh(g1)
        session.refresh(g2)
        return tid, g1.id, g2.id


def test_anonymous_pages_render(app_client, completed_match):
    tid, gid1, gid2 = completed_match
    r = app_client.get("/chessarena/")
    assert r.status_code == 200
    assert "Public Match" in r.text
    r = app_client.get("/chessarena/matches/")
    assert r.status_code == 200
    assert "Public Match" in r.text
    r = app_client.get(f"/chessarena/matches/{tid}")
    assert r.status_code == 200
    assert "Game" in r.text
    r = app_client.get(f"/chessarena/games/{gid1}")
    assert r.status_code == 200
    assert 'id="replay-root"' in r.text


def test_game_page_renders_react(app_client, completed_match):
    """The official /games/{id} replay mounts the React bundle and references
    the committed replay-app build output (no Lichess pgn-viewer)."""
    tid, gid1, gid2 = completed_match
    r = app_client.get(f"/chessarena/games/{gid1}")
    assert r.status_code == 200
    assert 'id="replay-root"' in r.text
    assert f"data-game-id=\"{gid1}\"" in r.text
    assert "/static/replay-app/assets/index.js" in r.text
    assert "pgn-viewer" not in r.text
    assert "lichess-pgn-viewer" not in r.text


def test_game_page_404s(app_client, completed_match, settings, engine_factory,
                        tournament_factory):
    _, gid1, _ = completed_match
    assert (
        app_client.get("/chessarena/games/not-a-real-id").status_code
        == 404
    )
    # A game in a DRAFT tournament must not be reachable.
    tid = tournament_factory(name="Draft", pairs=1, status=DRAFT)
    with engine_factory() as session:
        t = session.query(Tournament).filter(Tournament.id == tid).one()
        pair = t.pair_jobs[0]
        pgn_path = (
            settings.run_root / tid / "pairs" / "000000" / "attempt-01" / "match.pgn"
        )
        _write_match_pgn(pgn_path)
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
        session.commit()
        session.refresh(g)
        draft_gid = g.id
    assert (
        app_client.get(f"/chessarena/games/{draft_gid}").status_code
        == 404
    )


def test_public_api_lists_only_completed(app_client, completed_match, tournament_factory):
    tid, _, _ = completed_match
    # A non-completed tournament must not appear in the public list.
    draft_id = tournament_factory(name="Hidden Draft", pairs=1, status="DRAFT")
    cancelled_id = tournament_factory(
        name="Hidden Cancelled", pairs=1, status="CANCELLED"
    )
    r = app_client.get("/chessarena/public-api/v1/matches")
    assert r.status_code == 200
    ids = {m["id"] for m in r.json()}
    assert tid in ids
    assert draft_id not in ids
    assert cancelled_id not in ids


def test_public_api_whitelist_does_not_leak_internal_fields(
    app_client, completed_match
):
    tid, _, _ = completed_match
    r = app_client.get(f"/chessarena/public-api/v1/matches/{tid}")
    assert r.status_code == 200
    body = r.json()
    text = json.dumps(body)
    for forbidden in ("build_id", "binary_sha256", "git_sha", "config_snapshot",
                      "run_root", "pgn_path", "manifest", "command"):
        assert forbidden not in text, f"public API leaked {forbidden}"
    assert body["engine_a_label"]
    assert body["status"] == COMPLETED
    assert len(body["games"]) == 2
    for g in body["games"]:
        assert "verified" not in g  # verified flag is not part of the whitelist


def test_public_game_pgn_endpoint(app_client, completed_match):
    tid, gid1, gid2 = completed_match
    r = app_client.get(f"/chessarena/public-api/v1/games/{gid1}/pgn")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/x-chess-pgn")
    assert '[White "EngineA"]' in r.text
    assert '[Black "EngineB"]' in r.text
    assert "1-0" in r.text
    r2 = app_client.get(f"/chessarena/public-api/v1/games/{gid2}/pgn")
    assert r2.status_code == 200
    assert '[White "EngineB"]' in r2.text
    assert "0-1" in r2.text


def test_public_404s(app_client, completed_match):
    _, gid1, _ = completed_match
    assert app_client.get("/chessarena/public-api/v1/matches/not-a-real-id").status_code == 404
    assert app_client.get("/chessarena/public-api/v1/games/not-a-real-id/pgn").status_code == 404
    assert app_client.get("/chessarena/matches/not-a-real-id").status_code == 404
    assert app_client.get("/chessarena/games/not-a-real-id").status_code == 404


def test_unverified_game_not_public(settings, engine_factory, registered,
                                    tournament_factory, app_client):
    """A game whose pair failed must not be exposed even if the tournament
    somehow reports COMPLETED."""
    tid = tournament_factory(name="Partial", pairs=1, status=COMPLETED)
    with engine_factory() as session:
        t = session.query(Tournament).filter(Tournament.id == tid).one()
        t.finished_at = utcnow()
        pair = t.pair_jobs[0]
        pgn_path = (
            settings.run_root / tid / "pairs" / "000000" / "attempt-01" / "match.pgn"
        )
        _write_match_pgn(pgn_path)
        g = Game(
            tournament_id=tid,
            pair_job_id=pair.id,
            game_number=1,
            white_engine="EngineA",
            black_engine="EngineB",
            opening_index=0,
            result="1-0",
            pgn_path=str(pgn_path),
            verified=False,
        )
        session.add(g)
        session.commit()
        session.refresh(g)
        gid = g.id
    assert app_client.get(f"/chessarena/public-api/v1/games/{gid}/pgn").status_code == 404
    assert app_client.get(f"/chessarena/games/{gid}").status_code == 404
    detail = app_client.get(f"/chessarena/public-api/v1/matches/{tid}").json()
    assert detail["games"] == []


# ---------------------------------------------------------------------------
# P4.3 v1: live match status (unverified runtime data)
# ---------------------------------------------------------------------------
def _running_pair(settings, session, t, opening_fen, stdout_text):
    """Point pair 0 at a run dir with an opening.epd + stdout.log."""
    pair = t.pair_jobs[0]
    run_dir = settings.run_root / t.id / "pairs" / "000000" / "attempt-01"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "opening.epd").write_text(opening_fen + "\n", encoding="utf-8")
    (run_dir / "stdout.log").write_text(stdout_text, encoding="utf-8")
    pair.status = RUNNING
    pair.run_directory = str(run_dir)
    pair.started_at = utcnow()
    return pair


def test_live_idle_when_nothing_running(app_client):
    r = app_client.get("/chessarena/public-api/v1/live")
    assert r.status_code == 200
    assert r.json()["status"] == "idle"


def test_live_reports_running_pair(settings, engine_factory, tournament_factory,
                                   app_client):
    tid = tournament_factory(name="Live Match", pairs=2, status=RUNNING)
    opening_fen = (
        "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
    )
    with engine_factory() as session:
        t = session.query(Tournament).filter(Tournament.id == tid).one()
        _running_pair(
            settings, session, t, opening_fen,
            "Started game 1 of 2 (EngineA vs EngineB)\n",
        )
        session.commit()
    r = app_client.get("/chessarena/public-api/v1/live")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "live"
    assert body["tournament_id"] == tid
    assert body["name"] == "Live Match"
    assert body["pair_index"] == 0
    assert body["game_in_pair"] == 1
    assert body["games_total"] == 2
    assert body["state"] == "game_running"
    assert body["opening_fen"] == opening_fen
    assert body["pairs_total"] == 2
    # whitelist: no internal fields leak
    text = json.dumps(body)
    for forbidden in ("build_id", "binary_sha256", "git_sha", "run_root", "pgn_path"):
        assert forbidden not in text


def test_live_reports_last_result_and_pinned_id(
    settings, engine_factory, tournament_factory, app_client
):
    tid = tournament_factory(name="Live Match", pairs=1, status=RUNNING)
    opening_fen = (
        "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
    )
    stdout_text = (
        "Started game 1 of 2 (EngineA vs EngineB)\n"
        "Finished game 1 (EngineA vs EngineB): 1-0\n"
        "Started game 2 of 2 (EngineB vs EngineA)\n"
    )
    with engine_factory() as session:
        t = session.query(Tournament).filter(Tournament.id == tid).one()
        _running_pair(settings, session, t, opening_fen, stdout_text)
        session.commit()
    r = app_client.get(f"/chessarena/public-api/v1/live?tournament_id={tid}")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "live"
    assert body["game_in_pair"] == 2
    assert body["last_result"] == "1-0"


def test_live_completed_returns_match_url(app_client, completed_match):
    tid, _, _ = completed_match
    r = app_client.get(f"/chessarena/public-api/v1/live?tournament_id={tid}")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "completed"
    assert body["match_url"].endswith(f"/matches/{tid}")
    assert body["name"] == "Public Match"


def test_live_page_renders(app_client, tournament_factory):
    tid = tournament_factory(name="Live", pairs=1, status="RUNNING")
    r = app_client.get("/chessarena/live")
    assert r.status_code == 200
    assert 'data-mode="live"' in r.text
    assert 'id="replay-root"' in r.text
    assert "/static/replay-app/assets/index.js" in r.text
    r2 = app_client.get(f"/chessarena/live?tournament_id={tid}")
    assert r2.status_code == 200
    assert f'data-tournament-id="{tid}"' in r2.text
    assert app_client.get("/chessarena/live?tournament_id=not-a-real-id").status_code == 404
