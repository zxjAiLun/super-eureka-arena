"""V2.2-A regressions: experiment envelope + live decision view.

Covers: snapshot freezing, half-filled rejection, slug/stage validation,
fixed candidate/baseline mapping, both side kinds, the shared SPRT read
model, the pure-read status endpoint, fixed-pair semantics, and legacy
compatibility.
"""

from __future__ import annotations

import json

import pytest

from chessarena.models import COMPLETED, Tournament, utcnow
from chessarena.services import experiments, sprt as sprt_service


def _create_payload(app_client, opening_set_id, **overrides):
    payload = {
        "name": "exp-test",
        "engine_a": {"preset_id": "chessengine-production"},
        "engine_b": {"preset_id": "chessengine-legacy-current"},
        "opening_set_id": opening_set_id,
        "time_control": "blitz_3_2",
        "pairs": 2,
    }
    payload.update(overrides)
    return app_client.post("/chessarena/api/v1/tournaments", json=payload)


# ---------------------------------------------------------------------------
# (1) plain match: no experiment -> snapshot shape unchanged
# ---------------------------------------------------------------------------
def test_plain_match_snapshot_unchanged(app_client, engine_factory,
                                        registered):
    opening = json.loads(
        (registered["opening_dir"] / "manifest.json").read_text(encoding="utf-8")
    )
    r = _create_payload(app_client, opening["opening_set_id"])
    assert r.status_code == 201, r.text[:300]
    snap = r.json()["config_snapshot"]
    assert "experiment" not in snap
    # the frozen keys are exactly the pre-V2.2 set
    for key in ("engine_a", "engine_b", "opening_set", "time_control",
                "hash_mb", "threads", "concurrency", "requested_pairs"):
        assert key in snap


# ---------------------------------------------------------------------------
# (2) experiment envelope frozen verbatim
# ---------------------------------------------------------------------------
def test_experiment_envelope_frozen(app_client, engine_factory, registered):
    opening = json.loads(
        (registered["opening_dir"] / "manifest.json").read_text(encoding="utf-8")
    )
    r = _create_payload(
        app_client, opening["opening_set_id"],
        experiment={
            "experiment_id": "s9-c1-development-space",
            "purpose": "Confirm whether removing Development/Space helps.",
            "stage": "confirmation",
        })
    assert r.status_code == 201, r.text[:300]
    env = r.json()["config_snapshot"]["experiment"]
    assert env["schema_version"] == 1
    assert env["experiment_id"] == "s9-c1-development-space"
    assert env["purpose"] == "Confirm whether removing Development/Space helps."
    assert env["stage"] == "confirmation"
    assert env["candidate_side"] == "engine_a"
    assert env["baseline_side"] == "engine_b"
    assert env["decision_rule"] == "fixed_pairs"


# ---------------------------------------------------------------------------
# (3)+(4) half-filled / invalid stage / invalid slug -> 422
# ---------------------------------------------------------------------------
def test_experiment_validation_422(app_client, engine_factory, registered):
    opening = json.loads(
        (registered["opening_dir"] / "manifest.json").read_text(encoding="utf-8")
    )
    # half-filled (missing purpose)
    r = _create_payload(
        app_client, opening["opening_set_id"],
        experiment={"experiment_id": "x-1", "purpose": "", "stage": "screening"})
    assert r.status_code == 422
    # invalid stage
    r = _create_payload(
        app_client, opening["opening_set_id"],
        experiment={"experiment_id": "x-1", "purpose": "p", "stage": "random"})
    assert r.status_code == 422
    # invalid slug (uppercase)
    r = _create_payload(
        app_client, opening["opening_set_id"],
        experiment={"experiment_id": "Bad-ID", "purpose": "p",
                    "stage": "screening"})
    assert r.status_code == 422
    # invalid slug (spaces)
    r = _create_payload(
        app_client, opening["opening_set_id"],
        experiment={"experiment_id": "has space", "purpose": "p",
                    "stage": "screening"})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# (5) the side mapping is server-fixed; users cannot flip it
# ---------------------------------------------------------------------------
def test_candidate_side_always_a(app_client, engine_factory, registered):
    # The ExperimentConfig schema has no candidate_side/baseline_side
    # fields at all — the server freezes engine_a/engine_b unconditionally.
    from chessarena.schemas import ExperimentConfig

    cfg = ExperimentConfig(experiment_id="x-1", purpose="p",
                           stage="benchmark")
    dumped = cfg.model_dump()
    assert "candidate_side" not in dumped
    assert "baseline_side" not in dumped


# ---------------------------------------------------------------------------
# (6) version and preset sides both work as candidate/baseline
# ---------------------------------------------------------------------------
def test_version_and_preset_sides(app_client, engine_factory, registered):
    from chessarena.services import versions

    manifest = json.loads(
        (registered["build_dir"] / "manifest.json").read_text(encoding="utf-8")
    )
    with engine_factory() as session:
        versions.create_version_from_build(
            session, version_id="ce-cand-v", display_name="Cand V",
            build_id=manifest["build_id"], command_args=[],
            status="production", public_visible=True, rating_enabled=True,
        )
    opening = json.loads(
        (registered["opening_dir"] / "manifest.json").read_text(encoding="utf-8")
    )
    r = _create_payload(
        app_client, opening["opening_set_id"],
        engine_a={"version_id": "ce-cand-v"},
        engine_b={"preset_id": "chessengine-legacy-current"},
        experiment={"experiment_id": "ver-vs-preset", "purpose": "p",
                    "stage": "screening"})
    assert r.status_code == 201, r.text[:300]
    snap = r.json()["config_snapshot"]
    assert snap["engine_a"]["version_id"] == "ce-cand-v"
    assert snap["engine_b"]["preset_id"] == "chessengine-legacy-current"
    # experiment_view derives labels from the FROZEN snapshot
    with engine_factory() as session:
        t = session.query(Tournament).filter(
            Tournament.name == "exp-test").order_by(
            Tournament.created_at.desc()).first()
        view = experiments.experiment_view(t)
        assert view["candidate"]["version_id"] == "ce-cand-v"
        assert "Cand V" in experiments.side_display_name(view["candidate"])
        assert "Legacy" in experiments.side_display_name(view["baseline"])


# ---------------------------------------------------------------------------
# (7)+(8)+(9) shared SPRT read model agrees with the original computation
# ---------------------------------------------------------------------------
def _pair_job(w, l, d):
    return type("P", (), {
        "status": COMPLETED,
        "verification": {"candidate_perspective": {
            "wins": w, "losses": l, "draws": d}},
    })()


def test_pentanomial_matches_reference_vectors():
    """pentanomial_from_pairs reproduces the exact counts the scheduler
    previously computed inline (reference: ptnml buckets by pair score)."""
    pairs = [
        _pair_job(2, 0, 0),  # WW -> idx 4
        _pair_job(0, 2, 0),  # LL -> idx 0
        _pair_job(1, 1, 0),  # DD -> idx 2
        _pair_job(1, 0, 1),  # WD -> idx 3
        _pair_job(0, 1, 1),  # LD -> idx 1
        _pair_job(2, 0, 0),  # WW -> idx 4
    ]
    assert sprt_service.pentanomial_from_pairs(pairs) == [1, 1, 1, 1, 2]
    # non-completed pairs are skipped
    pending = type("P", (), {"status": "RUNNING", "verification": {}})()
    assert sprt_service.pentanomial_from_pairs(
        [pending] + pairs) == [1, 1, 1, 1, 2]


def test_tournament_sprt_state_matches_llr_implementation(engine_factory,
                                                          tournament_factory):
    """tournament_sprt_state() == sprt_llr_and_decision() on the same
    frozen contract + pair results (worker == UI decision)."""


def test_sprt_state_and_llr_agree(engine_factory, tournament_factory):
    """tournament_sprt_state() == sprt_llr_and_decision() on the same
    frozen contract + pair results (worker == UI decision)."""
    from chessarena.models import PairJob, Tournament as T

    tid = tournament_factory(name="sprt-agree", pairs=10, status=COMPLETED)
    cfg = {
        "enabled": True, "elo0": 0.0, "elo1": 10.0, "alpha": 0.05,
        "beta": 0.05, "max_pairs": 1000,
    }
    ptnml_target = [3, 5, 20, 8, 4]
    buckets = {0: (0, 2, 0), 1: (0, 1, 1), 2: (1, 1, 0),
               3: (1, 0, 1), 4: (2, 0, 0)}
    with engine_factory() as session:
        t = session.query(T).filter(T.id == tid).one()
        snap = dict(t.config_snapshot or {})
        snap["sprt"] = cfg
        snap["experiment"] = {
            "schema_version": 1, "experiment_id": "agree",
            "purpose": "p", "stage": "confirmation",
            "candidate_side": "engine_a", "baseline_side": "engine_b",
            "decision_rule": "sprt",
        }
        t.config_snapshot = snap
        # replace pair jobs with a synthetic verified set
        session.query(PairJob).filter(
            PairJob.tournament_id == tid).delete()
        i = 0
        for bucket, count in enumerate(ptnml_target):
            for _ in range(count):
                session.add(PairJob(
                    tournament_id=tid, pair_index=i, opening_index=i,
                    status=COMPLETED,
                    return_code=0,
                    verification={"candidate_perspective": {
                        "wins": buckets[bucket][0],
                        "losses": buckets[bucket][1],
                        "draws": buckets[bucket][2]}},
                ))
                i += 1
        t.completed_pairs = i
        t.status = COMPLETED
        session.commit()
        session.expire_all()

        t = session.query(T).filter(T.id == tid).one()
        state = sprt_service.tournament_sprt_state(t)
        ref = sprt_service.sprt_llr_and_decision(
            elo0=0.0, elo1=10.0, alpha=0.05, beta=0.05,
            ptnml=ptnml_target, max_pairs=1000)
        assert state is not None
        assert state["llr"] == ref["llr"]
        assert state["decision"] == ref["decision"]
        assert state["ptnml"] == ref["ptnml"] == ptnml_target
        assert state["pairs"] == ref["pairs"] == 40
        assert state["elo0"] == 0.0 and state["max_pairs"] == 1000

        # experiment_view maps the state and interpretation
        view = experiments.experiment_view(t)
        assert view["decision_rule"] == "sprt"
        assert view["sprt"]["llr"] == ref["llr"]
        assert view["state"] == "completed"
        assert view["candidate"] == snap["engine_a"]
        assert view["baseline"] == snap["engine_b"]


# ---------------------------------------------------------------------------
# (10) experiment-status endpoint: pure read, zero mutation
# ---------------------------------------------------------------------------
def test_status_endpoint_zero_mutation(app_client, engine_factory,
                                       registered, tournament_factory):
    from chessarena.models import PairJob, Tournament as T

    tid = tournament_factory(name="status-read", pairs=4, status=COMPLETED)
    with engine_factory() as session:
        t = session.query(T).filter(T.id == tid).one()
        snap = dict(t.config_snapshot or {})
        snap["experiment"] = {
            "schema_version": 1, "experiment_id": "status-read",
            "purpose": "read-only check", "stage": "screening",
            "candidate_side": "engine_a", "baseline_side": "engine_b",
            "decision_rule": "fixed_pairs",
        }
        t.config_snapshot = snap
        t.completed_pairs = 4
        t.candidate_wins = 3
        t.draws = 4
        t.candidate_losses = 1
        session.commit()
        before = json.dumps(snap, sort_keys=True)
        cw = t.candidate_wins

    r = app_client.get(
        f"/chessarena/admin/tournaments/{tid}/experiment-status")
    assert r.status_code == 200
    assert "status-read" in r.text
    assert "Fixed-pair measurement" in r.text
    assert "No formal decision" in r.text

    with engine_factory() as session:
        t = session.query(T).filter(T.id == tid).one()
        assert json.dumps(t.config_snapshot, sort_keys=True) == before
        assert t.candidate_wins == cw
        assert t.completed_pairs == 4


# ---------------------------------------------------------------------------
# (11) fixed-pair experiment never fakes PASS/FAIL
# ---------------------------------------------------------------------------
def test_fixed_pair_no_fake_decision(app_client, engine_factory, registered,
                                     tournament_factory):
    from chessarena.models import Tournament as T

    tid = tournament_factory(name="fixed-run", pairs=8, status=COMPLETED)
    with engine_factory() as session:
        t = session.query(T).filter(T.id == tid).one()
        snap = dict(t.config_snapshot or {})
        snap["experiment"] = {
            "schema_version": 1, "experiment_id": "fixed-run",
            "purpose": "measurement only", "stage": "benchmark",
            "candidate_side": "engine_a", "baseline_side": "engine_b",
            "decision_rule": "fixed_pairs",
        }
        t.config_snapshot = snap
        t.completed_pairs = 8
        t.candidate_wins = 6
        t.draws = 6
        t.candidate_losses = 4
        session.commit()
    r = app_client.get(
        f"/chessarena/admin/tournaments/{tid}/experiment-status")
    assert "Fixed-pair measurement" in r.text
    assert "No formal decision" in r.text
    for forbidden in ("PASS", "FAIL", "ACCEPT_H1", "ACCEPT_H0"):
        assert forbidden not in r.text, forbidden


# ---------------------------------------------------------------------------
# (12) legacy tournament: no envelope -> no panel, no errors
# ---------------------------------------------------------------------------
def test_legacy_tournament_no_panel(app_client, engine_factory, registered,
                                    tournament_factory):
    tid = tournament_factory(name="legacy-run", pairs=2, status=COMPLETED)
    with engine_factory() as session:
        t = session.query(Tournament).filter(
            Tournament.id == tid).one()
        assert "experiment" not in (t.config_snapshot or {})

    # detail page renders without the experiment panel
    r = app_client.get(f"/chessarena/admin/tournaments/{tid}")
    assert r.status_code == 200
    assert "Experiment" not in r.text

    # the status fragment renders empty (200, no panel)
    r = app_client.get(
        f"/chessarena/admin/tournaments/{tid}/experiment-status")
    assert r.status_code == 200
    assert "experiment-panel" not in r.text


# ---------------------------------------------------------------------------
# admin form: all-or-nothing envelope via the web surface
# ---------------------------------------------------------------------------
def test_admin_form_experiment_create(app_client, engine_factory, registered):
    app_client.get("/chessarena/admin/tournaments/new")  # csrf cookie
    token = app_client.cookies.get("arena_csrf")
    r = app_client.post(
        "/chessarena/admin/tournaments",
        data={
            "_csrf_token": token,
            "name": "admin-exp",
            "engine_a_side": "preset:chessengine-production",
            "engine_b_side": "preset:chessengine-legacy-current",
            "opening_set_id": "test-openings-v1",
            "time_control": "blitz_3_2",
            "pairs": "2",
            "experiment_enabled": "on",
            "experiment_id": "admin-form-exp",
            "experiment_stage": "screening",
            "experiment_purpose": "From the admin form.",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text[:300]
    with engine_factory() as session:
        t = session.query(Tournament).filter(
            Tournament.name == "admin-exp").one()
        env = t.config_snapshot["experiment"]
        assert env["experiment_id"] == "admin-form-exp"
        assert env["stage"] == "screening"

    # half-filled (checkbox on, empty fields) -> 422
    r = app_client.post(
        "/chessarena/admin/tournaments",
        data={
            "_csrf_token": token,
            "name": "admin-exp-bad",
            "engine_a_side": "preset:chessengine-production",
            "engine_b_side": "preset:chessengine-legacy-current",
            "opening_set_id": "test-openings-v1",
            "time_control": "blitz_3_2",
            "pairs": "2",
            "experiment_enabled": "on",
            "experiment_id": "",
            "experiment_stage": "screening",
            "experiment_purpose": "",
        },
        follow_redirects=False,
    )
    assert r.status_code == 422
    with engine_factory() as session:
        assert session.query(Tournament).filter(
            Tournament.name == "admin-exp-bad").first() is None


# ---------------------------------------------------------------------------
# run-again preserves the experiment envelope
# ---------------------------------------------------------------------------
def test_run_again_preserves_experiment(app_client, engine_factory,
                                        registered, tournament_factory):
    tid = tournament_factory(name="again-src", pairs=2, status=COMPLETED)
    with engine_factory() as session:
        t = session.query(Tournament).filter(
            Tournament.id == tid).one()
        snap = dict(t.config_snapshot or {})
        snap["experiment"] = {
            "schema_version": 1, "experiment_id": "again-exp",
            "purpose": "kept on rerun", "stage": "confirmation",
            "candidate_side": "engine_a", "baseline_side": "engine_b",
            "decision_rule": "fixed_pairs",
        }
        t.config_snapshot = snap
        session.commit()
    r = app_client.get(f"/chessarena/admin/tournaments/{tid}")
    assert "experiment_id=again-exp" in r.text
    assert "experiment_stage=confirmation" in r.text
    assert "kept+on+rerun" in r.text or "kept%20on%20rerun" in r.text
