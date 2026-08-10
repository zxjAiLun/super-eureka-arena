"""P4.11 commit 4 site IA tests: unified navigation, home vs matches
division, public ratings whitelist, site-wide W-D-L, Δ Elo display and
friendly time-control labels."""

from __future__ import annotations

import re

import pytest

from chessarena.models import COMPLETED, EngineBuild, Tournament, utcnow
from chessarena.services.display import (
    elo_delta_label,
    elo_delta_text,
    match_elo_delta,
    tc_label,
    wdl_text,
)

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


def _completed(settings, engine_factory, tournament_factory, name, *, wins,
               draws, losses, pairs=1, tc="blitz_3_2", arena_elo_enabled=False,
               anchor_vs_engine=False, with_game=False):
    from chessarena.models import Game

    tid = tournament_factory(
        name=name, pairs=pairs, time_control=tc, status=COMPLETED
    )
    with engine_factory() as session:
        t = session.query(Tournament).filter(Tournament.id == tid).one()
        t.completed_pairs = pairs
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


@pytest.fixture()
def completed_match(settings, engine_factory, tournament_factory):
    return _completed(settings, engine_factory, tournament_factory,
                      "Public IA Match", wins=1, draws=1, losses=0,
                      with_game=True)


# ---------------------------------------------------------------------------
# Δ Elo unit contracts (section 5)
# ---------------------------------------------------------------------------
class TestEloDelta:
    def test_symmetric_equal_score_is_zero(self):
        assert match_elo_delta(1, 0, 1) == 0
        assert elo_delta_text(0) == "0"

    def test_positive_for_engine_a_dominance(self):
        d = match_elo_delta(3, 2, 1)
        assert d is not None and d > 0
        assert elo_delta_text(d).startswith("+")

    def test_negative_for_engine_a_losses(self):
        d = match_elo_delta(1, 2, 3)
        assert d is not None and d < 0
        assert elo_delta_text(d).startswith("-")

    def test_all_wins_clamped(self):
        assert match_elo_delta(5, 0, 0) == 800
        assert elo_delta_text(800) == "+800"

    def test_all_losses_clamped(self):
        assert match_elo_delta(0, 0, 5) == -800
        assert elo_delta_text(-800) == "-800"

    def test_extreme_labels_are_bounds_not_exact(self):
        """100% / 0% scores are +∞ / −∞ mathematically: the display must
        read as a bound (≥+800 / ≤-800), never an exact value."""
        assert elo_delta_label(5, 0, 0) == "≥+800"
        assert elo_delta_label(0, 0, 5) == "≤-800"
        # Mid-range deltas still render as exact signed integers.
        assert elo_delta_label(3, 2, 1) == "+120"
        assert elo_delta_label(1, 2, 3) == "-120"
        assert elo_delta_label(1, 0, 1) == "0"
        assert elo_delta_label(0, 0, 0) == "—"

    def test_nothing_played(self):
        assert match_elo_delta(0, 0, 0) is None
        assert elo_delta_text(None) == "—"

    def test_perspective_not_inverted(self):
        """Engine A perspective: 3-2-1 positive, its mirror negative."""
        assert match_elo_delta(3, 2, 1) == -match_elo_delta(1, 2, 3)

    def test_wdl_text_order(self):
        assert wdl_text(3, 2, 1) == "3-2-1"


class TestTcLabel:
    def test_friendly_labels(self):
        assert tc_label("blitz_3_2") == "3+2"
        assert tc_label("bullet_1_0") == "1+0"
        assert tc_label("rapid_5_3") == "5+3"
        assert tc_label("blitz_10_01") == "10s+0.1s"

    def test_unknown_key_never_leaks_mapping(self):
        assert tc_label("nope") == "nope"


# ---------------------------------------------------------------------------
# Navigation contracts (section 1 + 8)
# ---------------------------------------------------------------------------
PUBLIC_LINKS = (
    "/chessarena/live",
    "/chessarena/matches/",
    "/chessarena/ratings/",
)


def test_public_pages_share_the_nav(app_client, completed_match):
    tid, gid = completed_match
    for path in ("/chessarena/", "/chessarena/matches/",
                 f"/chessarena/matches/{tid}", "/chessarena/live",
                 f"/chessarena/games/{gid}"):
        r = app_client.get(path)
        assert r.status_code == 200, path
        body = r.text
        assert 'class="brand" href="/chessarena/"' in body, path
        for link in PUBLIC_LINKS:
            assert f'href="{link}"' in body, (path, link)
        assert "/chessarena/admin/" in body, path


def test_admin_nav_has_public_order_plus_admin_actions(app_client,
                                                       completed_match):
    tid, _ = completed_match
    r = app_client.get("/chessarena/admin/")
    assert r.status_code == 200
    body = r.text
    # Common public entries in the same order as the public chrome.
    assert 'class="brand" href="/chessarena/"' in body
    for link in PUBLIC_LINKS:
        assert f'href="{link}"' in body
    assert 'href="/chessarena/admin/tournaments/new"' in body
    # Admin-only brand text is gone.
    assert "ChessArena v1" not in body
    # Admin can reach a public page directly.
    r2 = app_client.get("/chessarena/admin/tournaments/" + tid)
    assert r2.status_code == 200
    assert 'href="/chessarena/live"' in r2.text


# ---------------------------------------------------------------------------
# Home vs Matches division (section 2)
# ---------------------------------------------------------------------------
def test_home_is_overview_not_matches_copy(app_client, settings,
                                           engine_factory, tournament_factory):
    from datetime import timedelta

    for i in range(7):
        _completed(settings, engine_factory, tournament_factory,
                   f"recent-{i}", wins=2, draws=1, losses=1)
    # Deterministic ordering: distinct finish times, recent-6 newest.
    with engine_factory() as session:
        for i in range(7):
            t = session.query(Tournament).filter(
                Tournament.name == f"recent-{i}"
            ).one()
            t.finished_at = utcnow() - timedelta(minutes=7 - i)
        session.commit()

    home = app_client.get("/chessarena/")
    assert home.status_code == 200
    home_body = home.text
    assert "Live now" in home_body
    assert "No match currently live" in home_body
    assert "Latest result" in home_body
    assert "Recent matches" in home_body
    assert 'href="/chessarena/matches/"' in home_body
    assert 'href="/chessarena/ratings/"' in home_body
    # Compact: at most 5 recent rows (header row not counted).
    rows = re.findall(r"<tr>", home_body)
    assert len(rows) <= 6, f"home recent list too long: {len(rows)} rows"

    matches = app_client.get("/chessarena/matches/")
    assert matches.status_code == 200
    assert len(re.findall(r"<tr>", matches.text)) == 8, (
        "matches page must list the full history (7 matches)"
    )
    # A home row must never duplicate the matches table's scope: with 7
    # matches and a 5-row home list, at least the 2 oldest are only on the
    # full history page.
    for i in (0, 1):
        assert f"recent-{i}" not in home_body, (
            f"home must not copy the full table (found recent-{i})"
        )


# ---------------------------------------------------------------------------
# Public ratings whitelist (section 3)
# ---------------------------------------------------------------------------
def test_ratings_page_public_whitelist(app_client, settings, engine_factory,
                                       tournament_factory):
    _completed(settings, engine_factory, tournament_factory, "rated-ia",
               wins=6, draws=2, losses=2, pairs=5,
               arena_elo_enabled=True, anchor_vs_engine=True)
    r = app_client.get("/chessarena/ratings/")
    assert r.status_code == 200
    body = r.text
    assert "ChessEngine Production" in body
    assert "Stockfish Limited 2000" in body
    assert "Games" in body
    # Whitelist: nothing internal may reach the public page.
    for forbidden in ("fingerprint", "binary", "build_id", "stockfish-build",
                      "engine-build", "engine-sha", "uci_options",
                      "command_args", "run_root"):
        assert forbidden not in body, f"leaked: {forbidden}"
    # TC pools render with friendly labels.
    assert "3+2" in body and "5+3" in body


def test_admin_ratings_redirects_to_public(app_client):
    r = app_client.get("/chessarena/admin/ratings", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/chessarena/ratings/"


# ---------------------------------------------------------------------------
# Site-wide W-D-L and Δ Elo (section 4 + 7)
# ---------------------------------------------------------------------------
WDL_PAGES = (
    "/chessarena/",                # home recent rows + latest result
    "/chessarena/matches/",
    "/chessarena/admin/",
)


def test_wdl_order_everywhere(app_client, settings, engine_factory,
                              tournament_factory):
    tid, _ = _completed(settings, engine_factory, tournament_factory,
                        "wdl-match", wins=3, draws=2, losses=1)
    for path in WDL_PAGES:
        r = app_client.get(path)
        assert r.status_code == 200, path
        assert "3-2-1" in r.text, f"{path}: W-D-L missing"
        assert "3-1-2" not in r.text, f"{path}: W-L-D order leaked"

    detail = app_client.get(f"/chessarena/matches/{tid}")
    assert detail.status_code == 200
    assert "3-2-1" in detail.text
    assert "3-1-2" not in detail.text
    assert "Δ Elo (A−B)" in detail.text

    admin_detail = app_client.get(f"/chessarena/admin/tournaments/{tid}")
    assert admin_detail.status_code == 200
    assert "3-2-1" in admin_detail.text
    assert "3-1-2" not in admin_detail.text
    assert "Δ Elo (A−B)" in admin_detail.text


def test_no_legacy_score_text_anywhere(app_client, settings, engine_factory,
                                       tournament_factory):
    _completed(settings, engine_factory, tournament_factory, "legacy-scan",
               wins=1, draws=1, losses=1)
    for path in WDL_PAGES:
        r = app_client.get(path)
        assert "W/L/D" not in r.text, path
        assert "W / L / D" not in r.text, path
    # A specific completed match detail.
    matches = app_client.get("/chessarena/matches/").text
    m = re.search(r'href="/chessarena/matches/([0-9a-f-]+)"', matches)
    if m:
        r = app_client.get(f"/chessarena/matches/{m.group(1)}")
        assert "W/L/D" not in r.text
        assert "Score:" not in r.text


# ---------------------------------------------------------------------------
# Friendly time control (section 6)
# ---------------------------------------------------------------------------
def test_friendly_tc_on_public_pages(app_client, settings, engine_factory,
                                     tournament_factory):
    _completed(settings, engine_factory, tournament_factory, "tc-match",
               wins=1, draws=1, losses=1, tc="blitz_3_2")
    for path in ("/chessarena/", "/chessarena/matches/"):
        r = app_client.get(path)
        assert r.status_code == 200
        assert "3+2" in r.text, path
        assert "blitz_3_2" not in r.text, f"raw TC key leaked: {path}"
