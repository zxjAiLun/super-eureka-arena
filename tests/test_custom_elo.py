"""P4.6 Custom Stockfish Elo tests: per-match UCI_Elo override frozen into the
tournament snapshot, validated against the build's real capability schema,
displayed as the frozen label everywhere, and carried by Run again."""

from __future__ import annotations

import pytest

from chessarena.models import (
    COMPLETED,
    EngineBuild,
    EnginePreset,
    Tournament,
    utcnow,
)

ELO_SCHEMA = {
    "UCI_LimitStrength": {"type": "check", "default": "false"},
    "UCI_Elo": {"type": "spin", "default": "1350", "min": 1, "max": 2850},
    "Hash": {"type": "spin", "default": "16", "min": 1, "max": 1024},
}


@pytest.fixture()
def elo_build(engine_factory, registered):
    """Give the registered fake build a real UCI_Elo capability schema."""
    with engine_factory() as session:
        build = session.query(EngineBuild).first()
        build.uci_options_schema = dict(ELO_SCHEMA)
        session.commit()
    return build


def _create(app_client, custom_elo=None, engine="engine_b", **overrides):
    opening = app_client.get("/chessarena/api/v1/opening-sets").json()[0]
    payload = {
        "name": "custom-elo",
        "engine_a": {"preset_id": "chessengine-production"},
        "engine_b": {"preset_id": "chessengine-legacy-current"},
        "opening_set_id": opening["opening_set_id"],
        "time_control": "blitz_3_2",
        "pairs": 2,
        **overrides,
    }
    payload[engine] = {**payload[engine], "custom_elo": custom_elo}
    return app_client.post("/chessarena/api/v1/tournaments", json=payload)


def test_custom_elo_create_snapshot(app_client, engine_factory, elo_build):
    before = _count_presets(engine_factory)
    r = _create(app_client, custom_elo=1850)
    assert r.status_code == 201, r.text
    snap = r.json()["config_snapshot"]["engine_b"]
    assert snap["custom_elo"] == 1850
    assert snap["uci_options"]["UCI_LimitStrength"] is True
    assert snap["uci_options"]["UCI_Elo"] == 1850
    assert snap["display_name"].endswith(" 1850")
    # No EnginePreset is created for a per-match override.
    assert _count_presets(engine_factory) == before


def test_custom_elo_preset_unchanged_without_override(app_client, elo_build):
    r = _create(app_client, custom_elo=None)
    assert r.status_code == 201, r.text
    snap = r.json()["config_snapshot"]["engine_b"]
    assert "custom_elo" not in snap
    assert "UCI_Elo" not in snap["uci_options"]
    assert snap["display_name"] == "ChessEngine Legacy Baseline"


def test_custom_elo_out_of_range_422(app_client, elo_build):
    # Schema declares UCI_Elo max 2850.
    r = _create(app_client, custom_elo=3000)
    assert r.status_code == 422
    assert "maximum" in r.json()["detail"]


def test_custom_elo_unsupported_build_422(app_client, engine_factory,
                                          registered):
    """A preset whose build does not declare UCI_Elo must reject custom Elo."""
    with engine_factory() as session:
        build = session.query(EngineBuild).first()
        build.uci_options_schema = {"Hash": {"type": "spin"}}
        session.add(
            EnginePreset(
                preset_id="no-elo-preset",
                build_id=build.build_id,
                display_name="No Elo Engine",
                command_args=[],
                uci_options={},
                category="custom",
                public_visible=True,
                enabled=True,
            )
        )
        session.commit()
    r = _create(app_client, custom_elo=1500, engine="engine_b",
                **{"engine_b": {"preset_id": "no-elo-preset"}})
    assert r.status_code == 422
    assert "UCI_Elo" in r.json()["detail"]


def test_custom_elo_display_and_run_again(app_client, engine_factory, elo_build):
    tid = _create(app_client, custom_elo=1850).json()["id"]
    # Public match detail shows the frozen label.
    with engine_factory() as session:
        t = session.query(Tournament).filter(Tournament.id == tid).one()
        t.status = COMPLETED
        t.finished_at = utcnow()
        session.commit()
    detail = app_client.get(f"/chessarena/public-api/v1/matches/{tid}").json()
    assert detail["engine_b_label"] == "ChessEngine Legacy Baseline 1850"
    # Admin detail: Result summary label + Run again carries the Elo.
    admin = app_client.get(f"/chessarena/admin/tournaments/{tid}")
    assert "ChessEngine Legacy Baseline 1850" in admin.text
    assert "engine_b_elo=1850" in admin.text
    assert "engine_a_elo=" not in admin.text  # no override on engine_a


def test_new_match_page_custom_elo_ui(app_client, elo_build):
    """New Match page embeds per-preset Elo capability and prefills the
    per-match override from query params (Run again)."""
    r = app_client.get(
        "/chessarena/admin/tournaments/new?engine_b_elo=1850&engine_b_preset=chessengine-legacy-current"
    )
    assert r.status_code == 200
    assert 'name="engine_b_elo" min="1" value="1850"' in r.text
    assert 'name="engine_a_elo"' in r.text
    # Jinja tojson sorts object keys; assert the capability data is embedded.
    import re

    m = re.search(r"var PRESET_ELO = (\{.*?\});", r.text, re.S)
    assert m is not None, "PRESET_ELO not rendered"
    assert '"chessengine-legacy-current": {"max": 2850, "min": 1}' in m.group(1)


def _count_presets(engine_factory) -> int:
    with engine_factory() as session:
        return session.query(EnginePreset).count()
