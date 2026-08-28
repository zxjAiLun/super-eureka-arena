"""V2.2-B2: formal experiment wizard HTTP regressions.

Preview -> confirm lifecycle: zero-mutation preview, TOCTOU digest
guard, DRAFT-only creation, formal_protocol provenance.
"""

from __future__ import annotations

import json

from chessarena.models import (
    COMPLETED,
    EngineBuild,
    EnginePreset,
    Tournament,
)
from chessarena.services import versions


def _scene(engine_factory, registered):
    manifest = json.loads(
        (registered["build_dir"] / "manifest.json").read_text(encoding="utf-8")
    )
    import hashlib
    from pathlib import Path

    build_dir = Path(registered["build_dir"]).parent / "cand-build"
    build_dir.mkdir(parents=True, exist_ok=True)
    content = b"formal wizard candidate binary"
    (build_dir / "engine").write_bytes(content)
    m2 = {
        "build_id": "cand-build", "git_sha": "b" * 40,
        "binary_sha256": hashlib.sha256(content).hexdigest(),
    }
    with engine_factory() as session:
        session.add(EngineBuild(
            build_id="cand-build", engine_name="Test", git_sha=m2["git_sha"],
            binary_path=str(build_dir / "engine"),
            binary_sha256=m2["binary_sha256"], platform="x86_64",
            supported_profiles=[], manifest=m2, enabled=True,
        ))
        session.add(EnginePreset(
            preset_id="exp-candidate", build_id="cand-build",
            display_name="Experimental Candidate",
            command_args=[], uci_options={}, category="custom",
            public_visible=True, enabled=True,
        ))
        versions.create_version_from_build(
            session, version_id="ce-prod-baseline",
            display_name="Production Baseline",
            build_id=manifest["build_id"], command_args=[], uci_options={},
            status="production", rating_enabled=True, public_visible=True,
        )
        versions.set_channel(session, "current-final", "ce-prod-baseline")
        session.commit()


def _csrf(app_client):
    app_client.get("/chessarena/admin/experiments/formal/new")
    return app_client.cookies.get("arena_csrf")


def _form(token, **overrides):
    form = {
        "_csrf_token": token,
        "experiment_id": "s10-wizard-test",
        "experiment_stage": "confirmation",
        "experiment_purpose": "Wizard regression purpose.",
        "candidate": "preset:exp-candidate",
        "elo0": "0", "elo1": "10",
        "alpha": "0.05", "beta": "0.05", "max_pairs": "8",
        "opening_set_id": "test-openings-v1",
        "opening_plies": "", "opening_seed": "",
        "explicit_prior_tournament_ids": "",
    }
    form.update({k: str(v) for k, v in overrides.items()})
    return form


def test_preview_zero_mutation(app_client, engine_factory, registered):
    _scene(engine_factory, registered)
    token = _csrf(app_client)
    r = app_client.post(
        "/chessarena/admin/experiments/formal/preview", data=_form(token))
    assert r.status_code == 200, r.text[:300]
    body = r.text
    assert "s10-wizard-test" in body
    assert "Production Baseline" in body
    assert "ce-prod-baseline" in body  # resolved current-final baseline
    assert "Experimental Candidate" in body
    assert "Create formal experiment DRAFT" in body
    # seed generated once and echoed
    assert "Seed" in body
    # zero tournaments created by preview
    with engine_factory() as session:
        assert session.query(Tournament).filter(
            Tournament.name == "s10-wizard-test").first() is None


def test_preview_blocked_no_create_button(app_client, engine_factory,
                                           registered):
    _scene(engine_factory, registered)
    token = _csrf(app_client)
    r = app_client.post(
        "/chessarena/admin/experiments/formal/preview",
        data=_form(token, candidate="preset:no-such"))
    assert r.status_code == 200
    assert "Blocked" in r.text
    assert "Create formal experiment DRAFT" not in r.text


def _preview_and_extract(app_client, token, form):
    r = app_client.post(
        "/chessarena/admin/experiments/formal/preview", data=form)
    assert r.status_code == 200, r.text[:300]
    import re
    digest = re.search(
        r'name="plan_digest" value="([0-9a-f]{64})"', r.text).group(1)
    seed = re.search(
        r'name="opening_seed" value="(\d+)"', r.text).group(1)
    return digest, seed


def test_create_draft_with_matching_digest(app_client, engine_factory,
                                           registered):
    _scene(engine_factory, registered)
    token = _csrf(app_client)
    form = _form(token)
    digest, seed = _preview_and_extract(app_client, token, form)
    form.update({"opening_seed": seed, "plan_digest": digest})
    r = app_client.post(
        "/chessarena/admin/experiments/formal/create", data=form,
        follow_redirects=False)
    assert r.status_code == 303, r.text[:300]
    tid = r.headers["location"].rsplit("/", 1)[-1]
    with engine_factory() as session:
        t = session.get(Tournament, tid)
        # (21) DRAFT only — never enqueued
        assert t.status == "DRAFT"
        assert t.requested_pairs == 8
        assert t.arena_elo_enabled is False
        snap = t.config_snapshot
        env = snap["experiment"]
        assert env["experiment_id"] == "s10-wizard-test"
        assert env["stage"] == "confirmation"
        assert env["decision_rule"] == "sprt"
        assert snap["engine_b"]["version_id"] == "ce-prod-baseline"
        assert snap["engine_a"]["preset_id"] == "exp-candidate"
        assert snap["sprt"]["elo0"] == 0.0
        assert snap["sprt"]["elo1"] == 10.0
        assert snap["sprt"]["max_pairs"] == 8
        assert snap["sprt"]["enabled"] is True
        assert snap["sprt"]["unit"] == "pair"
        assert snap["sprt"]["model"] == "pentanomial"
        assert snap["sprt"]["elo_model"] == "logistic"
        # (22) formal_protocol provenance consistent with the snapshot
        fp = snap["formal_protocol"]
        assert fp["baseline_channel"] == "current-final"
        assert fp["excluded_fens_count"] == len(
            snap["sprt"].get("excluded_openings") or [])
        assert fp["plan_digest"] == digest
        import hashlib as h
        idx = snap["opening_set"]["indices"]
        assert fp["selected_indices_sha256"] == h.sha256(
            json.dumps(idx, sort_keys=True,
                       separators=(",", ":")).encode()).hexdigest()
        # seed frozen == preview seed
        assert snap["opening_set"]["seed"] == int(seed)


def test_create_rejects_digest_drift(app_client, engine_factory, registered):
    """(17) preview -> channel promoted elsewhere -> 409, zero creation."""
    _scene(engine_factory, registered)
    token = _csrf(app_client)
    form = _form(token)
    digest, seed = _preview_and_extract(app_client, token, form)
    form.update({"opening_seed": seed, "plan_digest": digest})

    # BETWEEN preview and create: promote a different version on
    # current-final (the baseline identity changes)
    import hashlib
    from pathlib import Path
    from chessarena.models import EngineBuild as EB

    build_dir = Path(registered["build_dir"]).parent / "newprod-build"
    build_dir.mkdir(parents=True, exist_ok=True)
    content = b"new production binary"
    (build_dir / "engine").write_bytes(content)
    m3 = {
        "build_id": "newprod-build", "git_sha": "e" * 40,
        "binary_sha256": hashlib.sha256(content).hexdigest(),
    }
    with engine_factory() as session:
        session.add(EB(
            build_id="newprod-build", engine_name="Test",
            git_sha=m3["git_sha"], binary_path=str(build_dir / "engine"),
            binary_sha256=m3["binary_sha256"], platform="x86_64",
            supported_profiles=[], manifest=m3, enabled=True,
        ))
        versions.create_version_from_build(
            session, version_id="ce-new-prod",
            display_name="New Production",
            build_id="newprod-build", command_args=[], uci_options={},
            status="candidate",
        )
        session.commit()
        versions.promote_channel(session, "current-final", "ce-new-prod")

    r = app_client.post(
        "/chessarena/admin/experiments/formal/create", data=form,
        follow_redirects=False)
    assert r.status_code == 409
    with engine_factory() as session:
        assert session.query(Tournament).filter(
            Tournament.name == "s10-wizard-test").first() is None


def test_create_rejects_missing_digest(app_client, engine_factory,
                                       registered):
    _scene(engine_factory, registered)
    token = _csrf(app_client)
    r = app_client.post(
        "/chessarena/admin/experiments/formal/create",
        data=_form(token), follow_redirects=False)
    assert r.status_code == 422


def test_create_rejects_drifted_preset(app_client, engine_factory,
                                       registered):
    """(18) preview -> candidate preset disabled -> 409/422, zero creation."""
    _scene(engine_factory, registered)
    token = _csrf(app_client)
    form = _form(token)
    digest, seed = _preview_and_extract(app_client, token, form)
    form.update({"opening_seed": seed, "plan_digest": digest})
    with engine_factory() as session:
        p = session.query(EnginePreset).filter(
            EnginePreset.preset_id == "exp-candidate").one()
        p.enabled = False
        session.commit()
    r = app_client.post(
        "/chessarena/admin/experiments/formal/create", data=form,
        follow_redirects=False)
    assert r.status_code in (409, 422)
    with engine_factory() as session:
        assert session.query(Tournament).filter(
            Tournament.name == "s10-wizard-test").first() is None


def test_csrf_required(app_client, engine_factory, registered):
    _scene(engine_factory, registered)
    _csrf(app_client)
    r = app_client.post(
        "/chessarena/admin/experiments/formal/preview",
        data=_form("wrong"), follow_redirects=False)
    assert r.status_code == 403
