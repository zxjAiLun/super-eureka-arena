"""P4.4 Fast Match Workflow tests: new-match defaults/prefill, last-used prefs,
and the completed-match result summary + Run again link."""

from __future__ import annotations

import re

from chessarena.api import tournaments as tournaments_api
from chessarena.models import COMPLETED, Tournament

NEW = "/chessarena/admin/tournaments/new"


def _norm(html: str) -> str:
    """Collapse inter-tag whitespace so multiline <option>s are assertable."""
    return re.sub(r"\s+", " ", html)


def test_new_match_renders_form(app_client):
    r = app_client.get(NEW)
    assert r.status_code == 200
    assert 'name="engine_a_preset"' in r.text
    assert 'name="engine_b_preset"' in r.text
    assert 'name="opening_set_id"' in r.text
    assert 'name="time_control"' in r.text
    assert 'name="pairs"' in r.text


def test_new_match_prefill_from_query(app_client):
    r = app_client.get(
        NEW
        + "?engine_a_preset=chessengine-production"
        + "&engine_b_preset=chessengine-legacy-current"
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
            "engine_a_preset": "chessengine-production",
            "engine_b_preset": "chessengine-legacy-current",
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
    assert "4 / 2 / 2" in r.text
    assert "Replay games" in r.text
    assert "Run again" in r.text
    # Run again prefills the original match parameters into new-match.
    assert f"/admin/tournaments/new?engine_a_preset=" in r.text
    assert "opening_set_id=test-openings-v1" in r.text
    assert "pairs=8" in r.text
