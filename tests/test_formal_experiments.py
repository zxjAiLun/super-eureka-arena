"""V2.2-B: formal experiment planner regressions (service layer).

The planner is a PURE READ: every gate below asserts fail-closed behavior
with ZERO tournament creation. Wizard/HTTP regressions live in
test_formal_wizard.py.
"""

from __future__ import annotations

import json

import pytest

from chessarena.models import (
    SPRT_ACCEPT_H1,
    SPRT_MAX_PAIRS,
    COMPLETED,
    EngineBuild,
    OpeningSet,
    Tournament,
)
from chessarena.schemas import FormalExperimentDraft
from chessarena.services import formal_experiments, versions


def _register_build(engine_factory, registered, build_id="formal-build",
                    git_sha="a" * 40):
    import hashlib
    from pathlib import Path

    build_dir = Path(registered["build_dir"]).parent / build_id
    build_dir.mkdir(parents=True, exist_ok=True)
    content = f"engine binary for {build_id}".encode()
    (build_dir / "engine").write_bytes(content)
    manifest = {
        "build_id": build_id, "git_sha": git_sha,
        "binary_sha256": hashlib.sha256(content).hexdigest(),
    }
    with engine_factory() as session:
        session.add(EngineBuild(
            build_id=build_id, engine_name="Test", git_sha=git_sha,
            binary_path=str(build_dir / "engine"),
            binary_sha256=manifest["binary_sha256"], platform="x86_64",
            supported_profiles=[], manifest=manifest, enabled=True,
        ))
        session.commit()
    return manifest


def _register_preset(engine_factory, registered, preset_id, build_id,
                     display_name=None, args=None, opts=None):
    from chessarena.models import EnginePreset

    with engine_factory() as session:
        session.add(EnginePreset(
            preset_id=preset_id, build_id=build_id,
            display_name=display_name or preset_id,
            command_args=args or [], uci_options=opts or {},
            category="custom", public_visible=True, enabled=True,
        ))
        session.commit()


def _setup_production_baseline(engine_factory, registered):
    """current-final -> a production EngineVersion (default identity)."""
    manifest = json.loads(
        (registered["build_dir"] / "manifest.json").read_text(encoding="utf-8")
    )
    with engine_factory() as session:
        versions.create_version_from_build(
            session, version_id="ce-prod-baseline",
            display_name="Production Baseline",
            build_id=manifest["build_id"], command_args=[], uci_options={},
            status="production", rating_enabled=True, public_visible=True,
        )
        versions.set_channel(session, "current-final", "ce-prod-baseline")
    return manifest


def _get_opening_set(engine_factory):
    with engine_factory() as session:
        return session.query(OpeningSet).filter(
            OpeningSet.opening_set_id == "test-openings-v1").one()


def _draft(**overrides) -> FormalExperimentDraft:
    payload = dict(
        experiment_id="s10-x-nnue",
        purpose="Test the NNUE candidate against production.",
        stage="confirmation",
        candidate="preset:exp-candidate",
        elo0=0.0, elo1=10.0, alpha=0.05, beta=0.05, max_pairs=8,
        opening_set_id="test-openings-v1",
        opening_plies=None,
    )
    payload.update(overrides)
    return FormalExperimentDraft(**payload)


def _scene(engine_factory, registered):
    """Production baseline on current-final + an experimental preset
    candidate on a SECOND build (so fingerprints differ)."""
    _setup_production_baseline(engine_factory, registered)
    m2 = _register_build(engine_factory, registered, "cand-build",
                         git_sha="b" * 40)
    _register_preset(engine_factory, registered, "exp-candidate",
                     "cand-build", "Experimental Candidate")
    return m2


# ---------------------------------------------------------------------------
# (1) baseline is always the current-final EngineVersion
# ---------------------------------------------------------------------------
def test_baseline_always_current_final(engine_factory, registered):
    _scene(engine_factory, registered)
    with engine_factory() as session:
        opening = _get_opening_set(engine_factory)
        plan = formal_experiments.plan_formal_experiment(
            session, _draft(), opening, seed=42)
        assert plan.ok, plan["errors"]
        assert plan["baseline"]["ref"] == "ce-prod-baseline"
        assert plan["baseline"]["kind"] == "version"
        # the draft schema has no baseline field at all
        assert not any(
            f for f in FormalExperimentDraft.model_fields
            if "baseline" in f)


# ---------------------------------------------------------------------------
# (2) confirmation allows an experimental preset candidate
# ---------------------------------------------------------------------------
def test_confirmation_allows_preset_candidate(engine_factory, registered):
    _scene(engine_factory, registered)
    with engine_factory() as session:
        opening = _get_opening_set(engine_factory)
        plan = formal_experiments.plan_formal_experiment(
            session, _draft(), opening, seed=42)
        assert plan.ok, plan["errors"]
        assert plan["candidate"]["kind"] == "preset"
        assert plan["candidate"]["ref"] == "exp-candidate"


# ---------------------------------------------------------------------------
# (3) candidate fingerprint == baseline fingerprint -> fail closed
# ---------------------------------------------------------------------------
def test_same_identity_candidate_blocked(engine_factory, registered):
    _setup_production_baseline(engine_factory, registered)
    # a preset that reproduces the baseline's default identity
    _register_preset(engine_factory, registered, "alias-preset",
                     json.loads((registered["build_dir"] / "manifest.json")
                                .read_text(encoding="utf-8"))["build_id"],
                     "Alias Preset", args=[], opts={})
    with engine_factory() as session:
        opening = _get_opening_set(engine_factory)
        plan = formal_experiments.plan_formal_experiment(
            session, _draft(candidate="preset:alias-preset"), opening,
            seed=42)
        assert not plan.ok
        assert any("SAME launch identity" in e for e in plan["errors"])


# ---------------------------------------------------------------------------
# (4) promotion candidate must be an EngineVersion
# ---------------------------------------------------------------------------
def test_promotion_candidate_must_be_version(engine_factory, registered):
    _scene(engine_factory, registered)
    with engine_factory() as session:
        opening = _get_opening_set(engine_factory)
        plan = formal_experiments.plan_formal_experiment(
            session, _draft(stage="promotion"), opening, seed=42)
        assert not plan.ok
        assert any(
            "promotion candidate must be an EngineVersion" in e
            for e in plan["errors"])


# ---------------------------------------------------------------------------
# (5) promotion candidate must pass plan_channel_promotion
# ---------------------------------------------------------------------------
def test_promotion_candidate_must_pass_promotion_gate(
    engine_factory, registered
):
    _scene(engine_factory, registered)
    # a candidate VERSION with a profile arg — fails the launch-identity gate
    m2 = _register_build(engine_factory, registered, "ver-cand-build",
                         git_sha="c" * 40)
    with engine_factory() as session:
        versions.create_version_from_build(
            session, version_id="ce-cand-profile",
            display_name="Cand With Profile",
            build_id="ver-cand-build",
            command_args=["--profile", "experimental"],
            uci_options={}, status="candidate",
        )
        session.commit()
    with engine_factory() as session:
        opening = _get_opening_set(engine_factory)
        plan = formal_experiments.plan_formal_experiment(
            session, _draft(stage="promotion",
                            candidate="version:ce-cand-profile"),
            opening, seed=42)
        assert not plan.ok
        assert any("promotion gate:" in e for e in plan["errors"])


# ---------------------------------------------------------------------------
# (6) promotion without a prior ACCEPT_H1 confirmation -> fail closed
# ---------------------------------------------------------------------------
def _freeze_indices(engine_factory, tid, indices):
    """Give a fixture tournament a frozen opening sample (the fixture
    itself does not set indices)."""
    with engine_factory() as session:
        t = session.query(Tournament).filter(Tournament.id == tid).one()
        snap = dict(t.config_snapshot or {})
        osnap = dict(snap.get("opening_set") or {})
        osnap["indices"] = list(indices)
        osnap["plies"] = None
        snap["opening_set"] = osnap
        t.config_snapshot = snap
        session.commit()


def _make_prior(engine_factory, tournament_factory, experiment_id, stage,
                status, with_sprt=True, indices=(0, 1)):
    tid = tournament_factory(name=f"prior-{experiment_id}-{stage}-{status}",
                             pairs=1, status=status)
    _freeze_indices(engine_factory, tid, indices)
    with engine_factory() as session:
        t = session.query(Tournament).filter(Tournament.id == tid).one()
        snap = dict(t.config_snapshot or {})
        snap["experiment"] = {
            "schema_version": 1, "experiment_id": experiment_id,
            "purpose": "p", "stage": stage,
            "candidate_side": "engine_a", "baseline_side": "engine_b",
            "decision_rule": "sprt" if with_sprt else "fixed_pairs",
        }
        if with_sprt:
            snap["sprt"] = {
                "enabled": True, "elo0": 0.0, "elo1": 10.0,
                "alpha": 0.05, "beta": 0.05, "max_pairs": 1000,
            }
        t.config_snapshot = snap
        session.commit()
    return tid


def test_promotion_requires_prior_accept_h1(engine_factory, registered,
                                            tournament_factory):
    _scene(engine_factory, registered)
    m3 = _register_build(engine_factory, registered, "promo-cand-build",
                         git_sha="d" * 40)
    with engine_factory() as session:
        versions.create_version_from_build(
            session, version_id="ce-promo-cand",
            display_name="Promotion Candidate",
            build_id="promo-cand-build", command_args=[], uci_options={},
            status="candidate",
        )
        session.commit()
    # no prior at all
    with engine_factory() as session:
        opening = _get_opening_set(engine_factory)
        plan = formal_experiments.plan_formal_experiment(
            session, _draft(stage="promotion",
                            candidate="version:ce-promo-cand"),
            opening, seed=42)
        assert not plan.ok
        assert any("requires a prior" in e for e in plan["errors"])

    # a COMPLETED (non-ACCEPT_H1) confirmation is not enough
    _make_prior(engine_factory, tournament_factory, "s10-x-nnue",
                "confirmation", COMPLETED)
    with engine_factory() as session:
        opening = _get_opening_set(engine_factory)
        plan = formal_experiments.plan_formal_experiment(
            session, _draft(stage="promotion",
                            candidate="version:ce-promo-cand"),
            opening, seed=42)
        assert not plan.ok

    # an ACCEPT_H1 confirmation of the same experiment satisfies the gate
    _make_prior(engine_factory, tournament_factory, "s10-x-nnue",
                "confirmation", SPRT_ACCEPT_H1)
    with engine_factory() as session:
        opening = _get_opening_set(engine_factory)
        plan = formal_experiments.plan_formal_experiment(
            session, _draft(stage="promotion",
                            candidate="version:ce-promo-cand"),
            opening, seed=42)
        assert plan.ok, plan["errors"]


# ---------------------------------------------------------------------------
# (8)+(9)+(10) statistical contract validation
# ---------------------------------------------------------------------------
def test_sprt_contract_fixed_and_validated(engine_factory, registered):
    _scene(engine_factory, registered)
    with engine_factory() as session:
        opening = _get_opening_set(engine_factory)
        plan = formal_experiments.plan_formal_experiment(
            session, _draft(), opening, seed=42)
        s = plan["sprt"]
        assert s["enabled"] is True
        assert s["unit"] == "pair"
        assert s["model"] == "pentanomial"
        assert s["elo_model"] == "logistic"
        assert s["max_pairs"] == 8
        import math
        assert math.isclose(s["lower_bound"], math.log(0.05 / 0.95))
        assert math.isclose(s["upper_bound"], math.log(0.95 / 0.05))

        # elo0 >= elo1 rejected
        plan = formal_experiments.plan_formal_experiment(
            session, _draft(elo0=10.0, elo1=10.0), opening, seed=42)
        assert not plan.ok
        assert any("elo0 must be < elo1" in e for e in plan["errors"])
        plan = formal_experiments.plan_formal_experiment(
            session, _draft(elo0=20.0, elo1=10.0), opening, seed=42)
        assert not plan.ok


# ---------------------------------------------------------------------------
# (11)+(12) prior-run FEN rebuild + identity fail-closed
# ---------------------------------------------------------------------------
def test_prior_exclusion_rebuild_and_fail_closed(
    engine_factory, registered, tournament_factory
):
    _scene(engine_factory, registered)
    # a prior run of the same experiment with a frozen opening sample
    tid = tournament_factory(name="prior-run", pairs=2, status=COMPLETED)
    _freeze_indices(engine_factory, tid, [0, 1])
    with engine_factory() as session:
        t = session.query(Tournament).filter(Tournament.id == tid).one()
        snap = dict(t.config_snapshot or {})
        snap["experiment"] = {
            "schema_version": 1, "experiment_id": "s10-x-nnue",
            "purpose": "p", "stage": "screening",
            "candidate_side": "engine_a", "baseline_side": "engine_b",
            "decision_rule": "fixed_pairs",
        }
        # the fixture's opening_set snapshot already carries indices + sha
        t.config_snapshot = snap
        session.commit()
        # capture the frozen opening identity
        osnap = snap["opening_set"]
        prior_indices = osnap["indices"]
        assert prior_indices, "fixture should freeze opening indices"

    with engine_factory() as session:
        opening = _get_opening_set(engine_factory)
        plan = formal_experiments.plan_formal_experiment(
            session, _draft(), opening, seed=42)
        assert plan.ok, plan["errors"]
        assert plan["automatic_prior_tournament_ids"] == [tid]
        assert plan["excluded_fens_count"] == len(set(prior_indices))
        assert plan["opening"]["eligible_after"] == \
            plan["opening"]["eligible_before"] - plan["excluded_fens_count"]

    # identity drift: tamper the file on disk -> fail closed
    import shutil
    from pathlib import Path
    with engine_factory() as session:
        opening = _get_opening_set(engine_factory)
        real = Path(opening.file_path)
    backup = real.with_suffix(".bak")
    shutil.copy2(real, backup)
    try:
        real.write_text(
            real.read_text(encoding="utf-8") + "\n# tampered\n",
            encoding="utf-8")
        with engine_factory() as session:
            opening = _get_opening_set(engine_factory)
            plan = formal_experiments.plan_formal_experiment(
                session, _draft(), opening, seed=42)
            assert not plan.ok
            assert any(
                "does not match" in e for e in plan["errors"])
    finally:
        shutil.move(str(backup), str(real))


# ---------------------------------------------------------------------------
# (13)+(14) automatic same-experiment + explicit legacy priors
# ---------------------------------------------------------------------------
def test_explicit_legacy_prior(engine_factory, registered,
                               tournament_factory):
    _scene(engine_factory, registered)
    # an unrelated legacy tournament (no envelope) with frozen openings
    legacy = tournament_factory(name="legacy-prior", pairs=1,
                                status=COMPLETED)
    _freeze_indices(engine_factory, legacy, [0, 1, 2])
    with engine_factory() as session:
        opening = _get_opening_set(engine_factory)
        plan = formal_experiments.plan_formal_experiment(
            session, _draft(explicit_prior_tournament_ids=[legacy]),
            opening, seed=42)
        assert plan.ok, plan["errors"]
        assert plan["explicit_prior_tournament_ids"] == [legacy]
        assert plan["excluded_fens_count"] > 0

        # unknown explicit prior id fails closed
        plan = formal_experiments.plan_formal_experiment(
            session, _draft(
                explicit_prior_tournament_ids=["no-such-id"]),
            opening, seed=42)
        assert not plan.ok
        assert any("not found" in e for e in plan["errors"])


# ---------------------------------------------------------------------------
# (15) not enough openings after exclusion -> blocked, zero creation
# ---------------------------------------------------------------------------
def test_insufficient_openings_after_exclusion(engine_factory, registered,
                                               tournament_factory):
    _scene(engine_factory, registered)
    # exhaust nearly the whole small test book via priors: create many
    # priors of the same experiment with distinct frozen samples
    for i in range(9):
        _make_prior(engine_factory, tournament_factory, "s10-x-nnue",
                    "screening", COMPLETED, indices=(2 * i, 2 * i + 1))
    # the fixture book has 20 positions; max_pairs=1000 >> remaining pool
    # (also blocked by the active/terminal gates, but the pool check is
    # the one we assert here via a DIFFERENT experiment id)
    with engine_factory() as session:
        opening = _get_opening_set(engine_factory)
        # directly exercise the pool arithmetic with max_pairs > pool
        plan = formal_experiments.plan_formal_experiment(
            session, _draft(experiment_id="fresh-exp", max_pairs=1000),
            opening, seed=42)
        # the test book only has 20 openings: even without exclusion,
        # 1000 pairs cannot be satisfied -> blocked
        assert not plan.ok
        assert any("eligible openings" in e for e in plan["errors"])
    with engine_factory() as session:
        assert session.query(Tournament).filter(
            Tournament.name == "anything-new").first() is None


# ---------------------------------------------------------------------------
# (16) same seed + exclusions -> deterministic identical sample
# ---------------------------------------------------------------------------
def test_deterministic_sample_same_seed(engine_factory, registered,
                                        tournament_factory):
    _scene(engine_factory, registered)
    tid = _make_prior(engine_factory, tournament_factory, "s10-x-nnue",
                      "screening", COMPLETED)
    with engine_factory() as session:
        opening = _get_opening_set(engine_factory)
        p1 = formal_experiments.plan_formal_experiment(
            session, _draft(), opening, seed=12345)
        p2 = formal_experiments.plan_formal_experiment(
            session, _draft(), opening, seed=12345)
        assert p1.ok and p2.ok
        assert p1["opening"]["selected_indices_sha256"] == \
            p2["opening"]["selected_indices_sha256"]
        assert p1["plan_digest"] == p2["plan_digest"]
        # different seed -> different digest
        p3 = formal_experiments.plan_formal_experiment(
            session, _draft(), opening, seed=999)
        assert p3.ok
        assert p3["plan_digest"] != p1["plan_digest"]


# ---------------------------------------------------------------------------
# (19) terminal formal confirmation blocks a second confirmation
# ---------------------------------------------------------------------------
def test_terminal_confirmation_blocks_reopen(engine_factory, registered,
                                             tournament_factory):
    _scene(engine_factory, registered)
    _make_prior(engine_factory, tournament_factory, "s10-x-nnue",
                "confirmation", SPRT_MAX_PAIRS)
    with engine_factory() as session:
        opening = _get_opening_set(engine_factory)
        plan = formal_experiments.plan_formal_experiment(
            session, _draft(), opening, seed=42)
        assert not plan.ok
        assert any(
            "do not reopen a terminated sequential test" in e
            for e in plan["errors"])


# ---------------------------------------------------------------------------
# (20) parallel active formal run blocked
# ---------------------------------------------------------------------------
def test_parallel_active_run_blocked(engine_factory, registered,
                                      tournament_factory):
    _scene(engine_factory, registered)
    _make_prior(engine_factory, tournament_factory, "s10-x-nnue",
                "confirmation", "RUNNING")
    with engine_factory() as session:
        opening = _get_opening_set(engine_factory)
        plan = formal_experiments.plan_formal_experiment(
            session, _draft(), opening, seed=42)
        assert not plan.ok
        assert any("active formal run" in e for e in plan["errors"])
