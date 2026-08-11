"""P4.4 Fast Match Workflow tests: new-match defaults/prefill, last-used prefs,
and the completed-match result summary + Run again link."""

from __future__ import annotations

import re

from chessarena.api import tournaments as tournaments_api
from chessarena.models import (
    COMPLETED,
    RUNNING,
    Event,
    Game,
    PairJob,
    Tournament,
    utcnow,
)

NEW = "/chessarena/admin/tournaments/new"


def _norm(html: str) -> str:
    """Collapse inter-tag whitespace so multiline <option>s are assertable."""
    return re.sub(r"\s+", " ", html)


def test_new_match_renders_form(app_client):
    r = app_client.get(NEW)
    assert r.status_code == 200
    assert 'name="engine_a_side"' in r.text
    assert 'name="engine_b_side"' in r.text
    assert 'name="opening_set_id"' in r.text
    assert 'name="time_control"' in r.text
    assert 'name="pairs"' in r.text


def test_new_match_prefill_from_query(app_client):
    r = app_client.get(
        NEW
        + "?engine_a_side=preset:chessengine-production"
        + "&engine_b_side=preset:chessengine-legacy-current"
        + "&opening_set_id=test-openings-v1"
        + "&opening_plies=12&time_control=rapid_5_3&pairs=4"
    )
    assert r.status_code == 200
    text = _norm(r.text)
    assert 'value="test-openings-v1" selected' in text
    assert 'name="opening_plies" min="1" value="12"' in text
    assert 'name="pairs" min="1" value="4"' in text
    assert 'value="rapid_5_3" selected' in text


def test_new_match_uses_last_used_prefs(settings, app_client):
    """Prefs persisted on create are read back as defaults for the next form."""
    tournaments_api._save_match_prefs(
        settings,
        {
            "engine_a_side": "preset:chessengine-production",
            "engine_b_side": "preset:chessengine-legacy-current",
            "opening_set_id": "test-openings-v1",
            "opening_plies": "14",
            "time_control": "bullet_1_0",
            "pairs": "6",
        },
    )
    r = app_client.get(NEW)
    assert r.status_code == 200
    text = _norm(r.text)
    assert 'value="test-openings-v1" selected' in text
    assert 'name="opening_plies" min="1" value="14"' in text
    assert 'name="pairs" min="1" value="6"' in text
    assert 'value="bullet_1_0" selected' in text


def test_completed_match_summary_and_run_again(
    app_client, settings, tournament_factory, engine_factory
):
    tid = tournament_factory(name="Run Again", pairs=8, status=COMPLETED)
    with engine_factory() as session:
        t = session.query(Tournament).filter(Tournament.id == tid).one()
        t.completed_pairs = 8
        t.candidate_wins = 4
        t.candidate_losses = 2
        t.draws = 2
        session.commit()
    r = app_client.get(f"/chessarena/admin/tournaments/{tid}")
    assert r.status_code == 200
    assert "Result" in r.text
    # P4.11 commit 4: W-D-L (wins-draws-losses), never W / L / D.
    assert "4-2-2" in r.text
    assert "4 / 2 / 2" not in r.text
    assert "Replay games" in r.text
    assert "Run again" in r.text
    assert "Delete match" in r.text
    # Run again prefills the original match parameters into new-match.
    assert f"/admin/tournaments/new?engine_a_side=" in r.text
    assert "opening_set_id=test-openings-v1" in r.text
    assert "pairs=8" in r.text


# ---------------------------------------------------------------------------
# P4.5b match delete
# ---------------------------------------------------------------------------
def _make_terminal_match(settings, engine_factory, tournament_factory, status):
    """Create a terminal/running tournament with a game, an event, and a run
    artifact directory so delete can be exercised end to end."""
    tid = tournament_factory(name="Del Me", pairs=1, status=status)
    with engine_factory() as session:
        t = session.query(Tournament).filter(Tournament.id == tid).one()
        t.completed_pairs = 1
        t.candidate_wins = 1
        t.finished_at = utcnow()
        pair = t.pair_jobs[0]
        pair.status = "COMPLETED"
        run_dir = settings.run_root / tid / "pairs" / "000000" / "attempt-01"
        run_dir.mkdir(parents=True, exist_ok=True)
        pgn_path = run_dir / "match.pgn"
        pgn_path.write_text("dummy", encoding="utf-8")
        pair.run_directory = str(run_dir)
        g = Game(
            tournament_id=tid,
            pair_job_id=pair.id,
            game_number=1,
            white_engine="A",
            black_engine="B",
            opening_index=0,
            result="1-0",
            pgn_path=str(pgn_path),
            verified=True,
        )
        ev = Event(tournament_id=tid, event_type="tournament_created", payload={})
        session.add_all([g, ev])
        session.commit()
    return tid


def _csrf(app_client):
    # GET any admin page to set the CSRF cookie.
    app_client.get("/chessarena/admin/tournaments/new")
    token = app_client.cookies.get("arena_csrf")
    assert token
    return token


def _counts(engine_factory, tid):
    with engine_factory() as session:
        return (
            session.query(Tournament).filter(Tournament.id == tid).first() is not None,
            session.query(Game).filter(Game.tournament_id == tid).first() is not None,
            session.query(Event).filter(Event.tournament_id == tid).first() is not None,
            session.query(PairJob).filter(PairJob.tournament_id == tid).first() is not None,
        )


def test_delete_completed_match(app_client, settings, engine_factory,
                                tournament_factory):
    tid = _make_terminal_match(settings, engine_factory, tournament_factory, COMPLETED)
    assert (settings.run_root / tid).exists()
    r = app_client.post(
        f"/chessarena/admin/tournaments/{tid}/delete",
        data={"_csrf_token": _csrf(app_client)},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert _counts(engine_factory, tid) == (False, False, False, False)
    assert not (settings.run_root / tid).exists()


def test_delete_running_rejected(app_client, settings, engine_factory,
                                 tournament_factory):
    tid = _make_terminal_match(settings, engine_factory, tournament_factory, RUNNING)
    r = app_client.post(
        f"/chessarena/admin/tournaments/{tid}/delete",
        data={"_csrf_token": _csrf(app_client)},
        follow_redirects=False,
    )
    assert r.status_code == 409
    # Nothing removed.
    assert _counts(engine_factory, tid) == (True, True, True, True)
    assert (settings.run_root / tid).exists()


def test_delete_not_found(app_client):
    r = app_client.post(
        "/chessarena/admin/tournaments/does-not-exist/delete",
        data={"_csrf_token": _csrf(app_client)},
        follow_redirects=False,
    )
    assert r.status_code == 404
