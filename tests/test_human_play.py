"""Human-play feature tests: allowlist, state machine, concurrency, abuse.

The engine side runs against the fake UCI engine fixture (a real subprocess
handshake), so the full create -> move -> worker-reply -> terminal flow is
exercised without a real Stockfish.
"""

from __future__ import annotations

import json
import os
from datetime import timedelta
from pathlib import Path

import chess
import pytest

from chessarena.config import Settings
from chessarena.models import HumanGame, HumanGameMove, utcnow
from chessarena.services import human_engine, human_game
from chessarena.services.human_play import OpponentError, resolve_opponent

from .conftest import BUILD_A_ID

FIXTURES = Path(__file__).parent / "fixtures"
FAKE_ENGINE = FIXTURES / "fake_legal_engine.py"

HUMAN_SETTINGS = dict(
    human_play_enabled=True,
    human_play_opponents=(
        "preset:stockfish-limited-1800,"
        "preset:stockfish-limited-2000,"
        "channel:current-final"
    ),
    human_play_movetime_ms=200,
    human_play_max_active_per_ip=2,
    human_play_max_created_per_hour=5,
    human_play_max_total_active=8,
    human_play_ttl_seconds=3600,
    human_play_idle_seconds=600,
    human_play_poll_seconds=0.05,
)


@pytest.fixture()
def hp_settings(settings: Settings) -> Settings:
    from dataclasses import replace

    return replace(settings, **HUMAN_SETTINGS)


@pytest.fixture()
def hp_registered(engine_factory, build_dir, registered):
    """The default build (via ``registered``) plus limited-strength presets
    and a channel."""
    from chessarena.models import EngineChannel, EnginePreset, EngineVersion

    manifest = json.loads((build_dir / "manifest.json").read_text())
    with engine_factory() as session:
        session.add(
            EnginePreset(
                preset_id="stockfish-limited-1800",
                build_id=BUILD_A_ID,
                display_name="Stockfish Limited 1800",
                command_args=[],
                uci_options={"UCI_LimitStrength": True, "UCI_Elo": 1800},
                category="external",
                public_visible=True,
                enabled=True,
            )
        )
        session.add(
            EnginePreset(
                preset_id="stockfish-limited-2000",
                build_id=BUILD_A_ID,
                display_name="Stockfish Limited 2000",
                command_args=[],
                uci_options={"UCI_LimitStrength": True, "UCI_Elo": 2000},
                category="external",
                public_visible=True,
                enabled=True,
            )
        )
        session.add(
            EngineVersion(
                version_id="ce-currentfinal-0001",
                display_name="ChessEngine CurrentFinal",
                build_id=BUILD_A_ID,
                command_args=["--profile", "current-final"],
                uci_options={},
                source_sha="a" * 40,
                binary_sha256=manifest["binary_sha256"],
                identity_fingerprint="f" * 64,
                status="production",
                rating_enabled=True,
                public_visible=True,
            )
        )
        session.add(
            EngineChannel(
                channel_id="current-final",
                engine_version_id="ce-currentfinal-0001",
            )
        )
        session.commit()
    return {
        "preset_1800": "preset:stockfish-limited-1800",
        "preset_2000": "preset:stockfish-limited-2000",
        "channel": "channel:current-final",
    }


@pytest.fixture()
def hp_app(hp_settings, hp_registered):
    """App client with the feature ON and a cross-origin-free test client.

    TestClient sends no Origin header by default, so require_same_origin
    passes; CSRF is exercised explicitly in dedicated tests.
    """
    from fastapi.testclient import TestClient

    from chessarena.main import create_app

    app = create_app(hp_settings)
    client = TestClient(app)
    return client


class _FakeRequest:
    """Just enough of Request for the human_game service."""

    def __init__(self, ip: str = "203.0.113.7"):
        self.headers = {"x-forwarded-for": ip}
        self.client = None


# ---------------------------------------------------------------------------
# Allowlist + resolution
# ---------------------------------------------------------------------------
def test_opponents_list_whitelisted_only(hp_app, hp_settings):
    r = hp_app.get("/chessarena/public-api/v1/human-play/opponents")
    assert r.status_code == 200
    body = r.json()
    assert [o["id"] for o in body] == [
        "preset:stockfish-limited-1800",
        "preset:stockfish-limited-2000",
        "channel:current-final",
    ]
    text = json.dumps(body)
    for forbidden in (
        "build_id", "binary_sha256", "binary_path", "uci_options",
        "preset_id", "version_id",
    ):
        assert forbidden not in text


def test_allowlist_rejects_public_but_not_allowed_preset(
    engine_factory, hp_registered
):
    """A registered, public, enabled preset that is NOT on the allowlist must
    be rejected — visibility is not human-play permission."""
    with engine_factory() as session:
        with pytest.raises(OpponentError):
            resolve_opponent(
                session,
                "preset:chessengine-production",
                (
                    "preset:stockfish-limited-1800",
                    "channel:current-final",
                ),
            )


def test_channel_resolution_freezes_version(engine_factory, hp_registered):
    with engine_factory() as session:
        choice = resolve_opponent(
            session, "channel:current-final",
            ("preset:stockfish-limited-1800", "channel:current-final"),
        )
        assert choice.version_id == "ce-currentfinal-0001"
        snap = choice.to_snapshot()
        # Promote the channel to a new version afterwards.
        from chessarena.models import EngineChannel, EngineVersion

        session.add(
            EngineVersion(
                version_id="ce-currentfinal-0002",
                display_name="ChessEngine CurrentFinal v2",
                build_id=BUILD_A_ID,
                command_args=[],
                uci_options={},
                source_sha="b" * 40,
                binary_sha256="0" * 64,
                identity_fingerprint="e" * 64,
                status="production",
                rating_enabled=True,
                public_visible=True,
            )
        )
        ch = (
            session.query(EngineChannel)
            .filter(EngineChannel.channel_id == "current-final")
            .one()
        )
        ch.engine_version_id = "ce-currentfinal-0002"
        session.commit()
        # The frozen snapshot is unaffected.
        assert snap["version_id"] == "ce-currentfinal-0001"
        # A NEW resolution follows the channel.
        choice2 = resolve_opponent(
            session, "channel:current-final",
            ("preset:stockfish-limited-1800", "channel:current-final"),
        )
        assert choice2.version_id == "ce-currentfinal-0002"


# ---------------------------------------------------------------------------
# Feature flag fail-closed
# ---------------------------------------------------------------------------
def test_feature_flag_off_404_everywhere(settings, hp_registered):
    from dataclasses import replace

    from fastapi.testclient import TestClient

    from chessarena.main import create_app

    off = replace(
        settings,
        human_play_enabled=False,
        human_play_opponents="preset:stockfish-limited-1800",
    )
    client = TestClient(create_app(off))
    assert client.get(
        "/chessarena/public-api/v1/human-play/opponents"
    ).status_code == 404
    assert client.get("/chessarena/human-play/").status_code == 404
    assert client.post(
        "/chessarena/public-api/v1/human-play/games",
        json={"opponent": "preset:stockfish-limited-1800",
              "human_color": "white"},
    ).status_code == 404
    assert client.get(
        "/chessarena/public-api/v1/human-play/games/x"
    ).status_code == 404
    assert client.post(
        "/chessarena/public-api/v1/human-play/games/x/moves",
        json={"uci": "e2e4", "expected_revision": 0},
    ).status_code == 404
    assert client.post(
        "/chessarena/public-api/v1/human-play/games/x/resign"
    ).status_code == 404
    assert client.get(
        "/chessarena/public-api/v1/human-play/games/x/pgn"
    ).status_code == 404


# ---------------------------------------------------------------------------
# Game creation + auth
# ---------------------------------------------------------------------------
class _Page:
    """Simulates the browser session: GET the page first (CSRF cookie set),
    then echo the token rendered into the page on state-changing calls."""

    def __init__(self, client):
        self.client = client
        r = client.get("/chessarena/human-play/")
        assert r.status_code == 200
        self.csrf = _extract_csrf(r.text)
        assert self.csrf

    def post(self, url, **kw):
        headers = kw.pop("headers", {})
        headers["X-CSRF-Token"] = self.csrf
        return self.client.post(url, headers=headers, **kw)

    def get(self, url, **kw):
        return self.client.get(url, **kw)


def _extract_csrf(html: str) -> str:
    import re

    m = re.search(r'data-csrf-token="([^"]+)"', html)
    return m.group(1) if m else ""


@pytest.fixture()
def page(hp_app):
    return _Page(hp_app)


def _create(page, opponent="preset:stockfish-limited-1800",
            color="white", extra_headers=None):
    headers = dict(extra_headers or {})
    return page.post(
        "/chessarena/public-api/v1/human-play/games",
        json={"opponent": opponent, "human_color": color},
        headers=headers,
    )


def test_create_game_returns_token_once(page):
    r = _create(page)
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "ACTIVE"
    assert body["revision"] == 0
    assert body["engine_pending"] is False
    assert body["fen"] == chess.STARTING_FEN
    assert body["game_token"]
    assert body["opponent_name"] == "Stockfish Limited 1800"
    # Token never appears again in state responses.
    r2 = page.get(
        f"/chessarena/public-api/v1/human-play/games/{body['id']}",
        headers={"X-Game-Token": body["game_token"]},
    )
    assert r2.status_code == 200
    assert "game_token" not in r2.json()


def test_create_requires_csrf_header(page):
    r = page.client.post(
        "/chessarena/public-api/v1/human-play/games",
        json={"opponent": "preset:stockfish-limited-1800",
              "human_color": "white"},
    )
    assert r.status_code == 403


def test_create_rejects_cross_origin(page):
    r = page.client.post(
        "/chessarena/public-api/v1/human-play/games",
        json={"opponent": "preset:stockfish-limited-1800",
              "human_color": "white"},
        headers={"X-CSRF-Token": page.csrf, "Origin": "https://evil.example"},
    )
    assert r.status_code == 403


def test_create_rejects_unknown_opponent(page):
    r = _create(page, opponent="preset:not-on-the-list")
    assert r.status_code == 404


def test_state_requires_valid_token(page):
    r = _create(page)
    gid = r.json()["id"]
    no_token = page.get(
        f"/chessarena/public-api/v1/human-play/games/{gid}"
    )
    assert no_token.status_code == 401
    bad = page.get(
        f"/chessarena/public-api/v1/human-play/games/{gid}",
        headers={"X-Game-Token": "0" * 64},
    )
    assert bad.status_code == 401
    missing = page.get(
        "/chessarena/public-api/v1/human-play/games/00000000-0000-0000-0000-000000000000",
        headers={"X-Game-Token": "whatever"},
    )
    # Unknown game and bad token are indistinguishable.
    assert missing.status_code == 401


# ---------------------------------------------------------------------------
# Rate limits
# ---------------------------------------------------------------------------
def test_per_ip_active_limit(page, hp_settings):
    for _ in range(hp_settings.human_play_max_active_per_ip):
        assert _create(page).status_code == 201
    assert _create(page).status_code == 429


def test_creation_hourly_limit(page, hp_settings, engine_factory):
    limit = hp_settings.human_play_max_created_per_hour
    # Finish games so the ACTIVE cap never trips; only the hourly counter.
    for i in range(limit):
        r = _create(page)
        assert r.status_code == 201
        b = r.json()
        page.post(
            f"/chessarena/public-api/v1/human-play/games/{b['id']}/resign",
            headers={"X-Game-Token": b["game_token"]},
        )
    assert _create(page).status_code == 429


def test_total_active_limit_spans_ips(page, hp_settings, engine_factory):
    made = 0
    ips = ["198.51.100.1", "198.51.100.2"]
    for ip in ips:
        for _ in range(hp_settings.human_play_max_active_per_ip):
            r = page.post(
                "/chessarena/public-api/v1/human-play/games",
                json={"opponent": "preset:stockfish-limited-1800",
                      "human_color": "white"},
                headers={"X-Forwarded-For": ip},
            )
            assert r.status_code == 201
            made += 1
    assert made == 4
    # Global cap is 8; fill to the cap from distinct IPs.
    for i in range(3):
        r = page.post(
            "/chessarena/public-api/v1/human-play/games",
            json={"opponent": "preset:stockfish-limited-1800",
                  "human_color": "white"},
            headers={"X-Forwarded-For": f"198.51.100.{10 + i}"},
        )
        assert r.status_code == 201
        made += 1
    assert made == 7
    r = page.post(
        "/chessarena/public-api/v1/human-play/games",
        json={"opponent": "preset:stockfish-limited-1800",
              "human_color": "white"},
        headers={"X-Forwarded-For": "198.51.100.99"},
    )
    assert r.status_code == 201  # 8th: at the cap but not over
    r = page.post(
        "/chessarena/public-api/v1/human-play/games",
        json={"opponent": "preset:stockfish-limited-1800",
              "human_color": "white"},
        headers={"X-Forwarded-For": "198.51.100.98"},
    )
    assert r.status_code == 503


# ---------------------------------------------------------------------------
# Move validation + optimistic concurrency
# ---------------------------------------------------------------------------
def _start(page, color="white"):
    r = _create(page, color=color)
    body = r.json()
    return body["id"], body["game_token"], body


def _move(page, gid, token, uci, revision):
    return page.post(
        f"/chessarena/public-api/v1/human-play/games/{gid}/moves",
        json={"uci": uci, "expected_revision": revision},
        headers={"X-Game-Token": token},
    )


def test_human_move_accepted_then_engine_pending(page):
    gid, token, body = _start(page)
    r = _move(page, gid, token, "e2e4", body["revision"])
    assert r.status_code == 200
    out = r.json()
    assert out["engine_pending"] is True
    assert out["revision"] == 1
    assert out["side_to_move"] == "black"


def test_human_move_illegal_rejected(page):
    gid, token, body = _start(page)
    assert _move(page, gid, token, "e2e5", 0).status_code == 400
    assert _move(page, gid, token, "e7e5", 0).status_code == 400  # not your side
    assert _move(page, gid, token, "zz9z9", 0).status_code == 422


def test_move_stale_revision_409(page):
    gid, token, _ = _start(page)
    assert _move(page, gid, token, "e2e4", 0).status_code == 200
    # Replay with the SAME revision: double-click / retry / second tab.
    assert _move(page, gid, token, "e2e4", 0).status_code == 409
    # And a stale-but-different move too.
    assert _move(page, gid, token, "d2d4", 0).status_code == 409


def test_move_while_engine_pending_409(page):
    gid, token, body = _start(page)
    assert _move(page, gid, token, "e2e4", 0).status_code == 200
    # Engine has not replied yet.
    assert _move(page, gid, token, "d2d4", 1).status_code == 409


def test_move_as_black_only_after_engine_white(page):
    gid, token, body = _start(page, color="black")
    # Human is black: first move must come from the engine, so a human move
    # immediately is rejected as "not your turn".
    assert _move(page, gid, token, "e7e5", 0).status_code == 409


# ---------------------------------------------------------------------------
# Worker engine executor (fake UCI engine)
# ---------------------------------------------------------------------------
def _use_fake_engine(engine_factory):
    from chessarena.models import EngineBuild

    def sha256_file(p: Path) -> str:
        import hashlib

        h = hashlib.sha256()
        h.update(p.read_bytes())
        return h.hexdigest()

    with engine_factory() as session:
        build = (
            session.query(EngineBuild)
            .filter(EngineBuild.build_id == BUILD_A_ID)
            .one()
        )
        build.binary_path = str(FAKE_ENGINE)
        build.binary_sha256 = sha256_file(FAKE_ENGINE)
        session.commit()
    os.chmod(FAKE_ENGINE, 0o755)


def test_worker_services_pending_move(hp_settings, engine_factory,
                                      hp_registered):
    _use_fake_engine(engine_factory)
    with engine_factory() as session:
        game, token = human_game.create_game(
            session, hp_settings, _FakeRequest(),
            "preset:stockfish-limited-1800", "white",
        )
        gid = game.id
        game = human_game.submit_human_move(
            session, hp_settings, session.get(HumanGame, gid), "e2e4", 0
        )
        assert game.engine_pending is True

    with engine_factory() as session:
        action = human_engine.service_pending_move(
            hp_settings, session, session.get(HumanGame, gid)
        )
        assert "played" in action
    with engine_factory() as session:
        game = session.get(HumanGame, gid)
        assert game.engine_pending is False
        assert game.revision == 2
        moves = (
            session.query(HumanGameMove)
            .filter(HumanGameMove.human_game_id == gid)
            .order_by(HumanGameMove.ply)
            .all()
        )
        assert [m.uci for m in moves] == ["e2e4", "g8h6"]  # fake plays Nh6
        assert moves[1].side == "engine"
        assert moves[1].engine_ms is not None


def test_worker_move_api_roundtrip(hp_settings, engine_factory, page,
                                   hp_registered):
    _use_fake_engine(engine_factory)
    gid, token, body = _start(page)
    assert _move(page, gid, token, "e2e4", body["revision"]).status_code == 200
    with engine_factory() as session:
        game = session.get(HumanGame, gid)
        assert game.engine_pending is True
    with engine_factory() as session:
        action = human_engine.service_pending_move(
            hp_settings, session, session.get(HumanGame, gid)
        )
        assert "played" in action
    r = page.get(
        f"/chessarena/public-api/v1/human-play/games/{gid}",
        headers={"X-Game-Token": token},
    )
    out = r.json()
    assert out["engine_pending"] is False
    assert out["revision"] == 2
    assert len(out["moves"]) == 2
    assert out["moves"][1]["side"] == "engine"


# ---------------------------------------------------------------------------
# Resign, expiry, PGN
# ---------------------------------------------------------------------------
def test_resign_and_pgn(page, hp_settings, engine_factory, hp_registered):
    gid, token, body = _start(page)
    _move(page, gid, token, "e2e4", body["revision"])
    r = page.post(
        f"/chessarena/public-api/v1/human-play/games/{gid}/resign",
        headers={"X-Game-Token": token},
    )
    assert r.status_code == 200
    out = r.json()
    assert out["status"] == "RESIGNED"
    assert out["result"] == "0-1"  # human was white
    pgn = page.get(
        f"/chessarena/public-api/v1/human-play/games/{gid}/pgn",
        headers={"X-Game-Token": token},
    )
    assert pgn.status_code == 200
    text = pgn.text
    assert '[White "Human"]' in text
    assert '[Black "Stockfish Limited 1800"]' in text
    assert "1. e4" in text
    assert '[Result "0-1"]' in text


def test_pgn_requires_terminal(page):
    gid, token, _ = _start(page)
    r = page.get(
        f"/chessarena/public-api/v1/human-play/games/{gid}/pgn",
        headers={"X-Game-Token": token},
    )
    assert r.status_code == 409


def test_lazy_idle_expiry(hp_settings, engine_factory, hp_registered):
    with engine_factory() as session:
        game, token = human_game.create_game(
            session, hp_settings, _FakeRequest(),
            "preset:stockfish-limited-1800", "white",
        )
        gid = game.id
        # Force the idle deadline into the past.
        game.idle_expires_at = utcnow() - timedelta(seconds=1)
        session.commit()
    with engine_factory() as session:
        game = session.get(HumanGame, gid)
        human_game.apply_lazy_expiry(session, game)
        session.commit()
    with engine_factory() as session:
        game = session.get(HumanGame, gid)
        assert game.status == "EXPIRED"
        assert game.termination == "idle_expired"
        assert game.result is None


def test_expired_game_rejects_moves(page, hp_settings, engine_factory,
                                    hp_registered):
    gid, token, body = _start(page)
    with engine_factory() as session:
        game = session.get(HumanGame, gid)
        game.idle_expires_at = utcnow() - timedelta(seconds=1)
        session.commit()
    r = _move(page, gid, token, "e2e4", body["revision"])
    assert r.status_code == 410


def test_mate_ends_game_with_pgn(hp_settings, engine_factory, hp_registered):
    """Drive Scholar-like quick mate: human e4/Qh5/Bc4/Qxf7# while a stubbed
    engine reply sequence walks into it."""
    calls = {"n": 0}

    def fake_ask(snapshot, moves, movetime_ms):
        calls["n"] += 1
        reply_seq = ["a7a6", "b7b6", "c7c6"]
        return reply_seq[calls["n"] - 1], 42

    orig = human_engine.ask_engine_move
    human_engine.ask_engine_move = fake_ask
    gid = None
    try:
        with engine_factory() as session:
            game, token = human_game.create_game(
                session, hp_settings, _FakeRequest(),
                "preset:stockfish-limited-1800", "white",
            )
            gid = game.id
            revision = 0
            for human_uci in ["e2e4", "d1h5", "f1c4", "h5f7"]:
                game = human_game.submit_human_move(
                    session, hp_settings, session.get(HumanGame, gid),
                    human_uci, revision,
                )
                revision = game.revision
                if game.status == "FINISHED":
                    break
                session.expire_all()
                action = human_engine.service_pending_move(
                    hp_settings, session, session.get(HumanGame, gid)
                )
                session.expire_all()
                game = session.get(HumanGame, gid)
                revision = game.revision
                if game.status == "FINISHED":
                    break
            assert game.status == "FINISHED"
            assert game.result == "1-0"
            assert game.termination == "checkmate"
    finally:
        human_engine.ask_engine_move = orig
    with engine_factory() as session:
        game = session.get(HumanGame, gid)
        text = human_game.ensure_pgn(session, hp_settings, game)
    assert "Qxf7#" in text
    assert '[Result "1-0"]' in text


# ---------------------------------------------------------------------------
# Worker arbitration contract (the experiment-correctness invariant)
# ---------------------------------------------------------------------------
def test_human_move_never_runs_during_active_pair(hp_settings, engine_factory,
                                                  hp_registered):
    """_worker_step must NOT service a human move while a pair is running;
    only between pairs, and only after the scheduler tick reports idle."""
    import subprocess

    from chessarena.services.scheduler import Scheduler
    from chessarena.worker import _worker_step

    _use_fake_engine(engine_factory)
    scheduler = Scheduler(hp_settings, engine_factory)
    # Simulate a running cutechess pair: scheduler.active_proc set.
    sleeper = subprocess.Popen(
        ["sleep", "30"], start_new_session=True
    )
    scheduler.active_proc = sleeper
    scheduler.active_tournament_id = "t"
    scheduler.active_pair_job_id = "p"
    try:
        with engine_factory() as session:
            game, token = human_game.create_game(
                session, hp_settings, _FakeRequest(),
                "preset:stockfish-limited-1800", "white",
            )
            gid = game.id
            human_game.submit_human_move(
                session, hp_settings, session.get(HumanGame, gid), "e2e4", 0
            )
        action, _ = _worker_step(
            hp_settings, engine_factory, scheduler, None
        )
        assert action == "pair running: p"
        with engine_factory() as session:
            game = session.get(HumanGame, gid)
            assert game.engine_pending is True  # untouched while pair runs
    finally:
        sleeper.terminate()
        sleeper.wait(timeout=10)
        scheduler.active_proc = None


def test_worker_step_services_human_move_when_idle(hp_settings,
                                                   engine_factory,
                                                   hp_registered):
    from chessarena.services.scheduler import Scheduler
    from chessarena.worker import _worker_step

    _use_fake_engine(engine_factory)
    scheduler = Scheduler(hp_settings, engine_factory)
    with engine_factory() as session:
        game, token = human_game.create_game(
            session, hp_settings, _FakeRequest(),
            "preset:stockfish-limited-1800", "white",
        )
        gid = game.id
        human_game.submit_human_move(
            session, hp_settings, session.get(HumanGame, gid), "e2e4", 0
        )
    action, _ = _worker_step(hp_settings, engine_factory, scheduler, None)
    assert action == "human-move"
    with engine_factory() as session:
        game = session.get(HumanGame, gid)
        assert game.engine_pending is False


def test_worker_step_skips_human_play_when_disabled(hp_settings,
                                                    engine_factory,
                                                    hp_registered):
    from dataclasses import replace

    from chessarena.services.scheduler import Scheduler
    from chessarena.worker import _worker_step

    _use_fake_engine(engine_factory)
    off = replace(hp_settings, human_play_enabled=False)
    scheduler = Scheduler(off, engine_factory)
    with engine_factory() as session:
        game, token = human_game.create_game(
            session, hp_settings, _FakeRequest(),
            "preset:stockfish-limited-1800", "white",
        )
        gid = game.id
        human_game.submit_human_move(
            session, hp_settings, session.get(HumanGame, gid), "e2e4", 0
        )
    action, _ = _worker_step(off, engine_factory, scheduler, None)
    assert action == "idle"  # flag off: no engine spawned


def test_engine_failure_marks_game(hp_settings, engine_factory,
                                   hp_registered):
    from chessarena.services.human_engine import EngineReplyError

    def broken(snapshot, moves, movetime_ms):
        raise EngineReplyError("boom")

    orig = human_engine.ask_engine_move
    human_engine.ask_engine_move = broken
    try:
        with engine_factory() as session:
            game, token = human_game.create_game(
                session, hp_settings, _FakeRequest(),
                "preset:stockfish-limited-1800", "white",
            )
            gid = game.id
            human_game.submit_human_move(
                session, hp_settings, session.get(HumanGame, gid), "e2e4", 0
            )
        with engine_factory() as session:
            action = human_engine.service_pending_move(
                hp_settings, session, session.get(HumanGame, gid)
            )
            assert "failed" in action
            game = session.get(HumanGame, gid)
            assert game.status == "ENGINE_FAILED"
            assert game.engine_pending is False
    finally:
        human_engine.ask_engine_move = orig


# ---------------------------------------------------------------------------
# H5 hardening: recovery, movetime cap, PGN whitelist
# ---------------------------------------------------------------------------
def test_worker_restart_recovery_service_pending(hp_settings, engine_factory,
                                                 hp_registered):
    """A pending engine move survives a worker restart: the flag lives in
    the DB, not in memory, so the next worker services it."""
    _use_fake_engine(engine_factory)
    with engine_factory() as session:
        game, token = human_game.create_game(
            session, hp_settings, _FakeRequest(),
            "preset:stockfish-limited-1800", "white",
        )
        gid = game.id
        human_game.submit_human_move(
            session, hp_settings, session.get(HumanGame, gid), "e2e4", 0
        )
    # Simulate: worker A died right after the human move was accepted.
    # Worker B boots later and finds the pending flag in the DB.
    with engine_factory() as session:
        game = session.get(HumanGame, gid)
        assert game.engine_pending is True
        action = human_engine.service_pending_move(
            hp_settings, session, game
        )
        assert "played" in action
        game = session.get(HumanGame, gid)
        assert game.engine_pending is False
        assert game.revision == 2


def test_movetime_is_server_controlled(hp_settings, engine_factory,
                                       hp_registered):
    """The movetime comes from settings only; the API accepts no time
    parameter at all (schema has no such field -> 422 if attempted)."""
    _use_fake_engine(engine_factory)
    with engine_factory() as session:
        game, token = human_game.create_game(
            session, hp_settings, _FakeRequest(),
            "preset:stockfish-limited-1800", "white",
        )
        gid = game.id
        human_game.submit_human_move(
            session, hp_settings, session.get(HumanGame, gid), "e2e4", 0
        )
    captured = {}
    orig = human_engine.ask_engine_move

    def spy(snapshot, moves, movetime_ms):
        captured["movetime_ms"] = movetime_ms
        return orig(snapshot, moves, movetime_ms)

    human_engine.ask_engine_move = spy
    try:
        with engine_factory() as session:
            human_engine.service_pending_move(
                hp_settings, session, session.get(HumanGame, gid)
            )
        # Exactly the configured budget — never client-influenced.
        assert captured["movetime_ms"] == hp_settings.human_play_movetime_ms
    finally:
        human_engine.ask_engine_move = orig


def test_pgn_content_whitelist(hp_settings, engine_factory, hp_registered):
    """The synthesized PGN must not leak build ids, SHAs or paths."""
    with engine_factory() as session:
        game, token = human_game.create_game(
            session, hp_settings, _FakeRequest(),
            "channel:current-final", "black",
        )
        gid = game.id
        human_game.resign_game(session, hp_settings, session.get(HumanGame, gid))
        text = human_game.ensure_pgn(session, hp_settings, game)
    for forbidden in (
        "build_id", "binary_sha256", "binary_path", "/opt/", "uci_options",
    ):
        assert forbidden not in text, forbidden
    # Human resigned as black -> engine (white) wins.
    assert '[White "ChessEngine CurrentFinal"]' in text
    assert '[Black "Human"]' in text
    assert '[Result "1-0"]' in text
    assert '[Termination "resign"]' in text


def test_ttl_expiry_path(hp_settings, engine_factory, hp_registered):
    with engine_factory() as session:
        game, token = human_game.create_game(
            session, hp_settings, _FakeRequest(),
            "preset:stockfish-limited-1800", "white",
        )
        gid = game.id
        game.expires_at = utcnow() - timedelta(seconds=1)
        session.commit()
    with engine_factory() as session:
        game = human_game.get_game(session, hp_settings, gid, token)
        assert game.status == "EXPIRED"
        assert game.termination == "ttl_expired"


def test_state_payload_never_leaks_provenance(hp_settings, engine_factory,
                                              hp_registered):
    with engine_factory() as session:
        game, token = human_game.create_game(
            session, hp_settings, _FakeRequest(),
            "preset:stockfish-limited-1800", "white",
        )
        payload = human_game.game_payload(game)
    text = json.dumps(payload)
    for forbidden in (
        "build_id", "binary_sha256", "binary_path", "uci_options",
        "creator_ip", "game_token_hash", "command_args", "version_id",
        "preset_id",
    ):
        assert forbidden not in text, forbidden


def test_human_move_prioritized_over_next_queued_pair(
    hp_settings, engine_factory, hp_registered, tournament_factory
):
    """Between pairs: one pending human move is serviced BEFORE the next
    queued tournament pair launches (bounded wall-clock delay, no concurrent
    computation) — the user-specified arbitration order."""
    from chessarena.services.scheduler import Scheduler
    from chessarena.worker import _worker_step

    # Stub the engine reply so the registered build stays untouched (a
    # queued pair must still pass its frozen-SHA launch checks).
    def fake_ask(snapshot, moves, movetime_ms):
        return "g8f6", 30

    orig = human_engine.ask_engine_move
    human_engine.ask_engine_move = fake_ask
    try:
        tid = tournament_factory(name="queued match", pairs=1,
                                 status="QUEUED")
        with engine_factory() as session:
            game, token = human_game.create_game(
                session, hp_settings, _FakeRequest(),
                "preset:stockfish-limited-1800", "white",
            )
            gid = game.id
            human_game.submit_human_move(
                session, hp_settings, session.get(HumanGame, gid), "e2e4", 0
            )
        scheduler = Scheduler(hp_settings, engine_factory)
        # First step: the human move must be answered, NOT the pair launched.
        action, _ = _worker_step(hp_settings, engine_factory, scheduler, None)
        assert action == "human-move"
        with engine_factory() as session:
            game = session.get(HumanGame, gid)
            assert game.engine_pending is False
        assert scheduler.active_proc is None  # no pair started yet
        # Second step: now the queued pair launches.
        action, _ = _worker_step(hp_settings, engine_factory, scheduler, None)
        assert action.startswith("launched pair"), (
            f"unexpected action: {action!r}"
        )
        scheduler.shutdown()
    finally:
        human_engine.ask_engine_move = orig
