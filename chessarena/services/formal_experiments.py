"""V2.2-B: formal experiment planner — pure-read preview of a formal
confirmation/promotion SPRT contract.

The planner composes EXISTING primitives into the formal contract:

- baseline: ALWAYS resolved server-side from channel:current-final to the
  immutable EngineVersion it points at (users cannot choose it, killing
  the preset-alias mis-selection class);
- candidate: preset (experimental profile) or candidate/experimental
  EngineVersion for confirmation; strictly an EngineVersion passing the
  full V2.1 promotion gate for promotion;
- statistical model: fixed pentanomial/pair/logistic SPRT; only the
  hypothesis parameters (elo0/elo1/alpha/beta/max_pairs) are inputs;
- opening independence: prior runs' frozen opening samples are rebuilt
  from their snapshots (fail-closed on any identity mismatch) and
  excluded from the deterministic sample;
- stage gates: no second formal confirmation of a terminated sequential
  test; promotion requires a prior ACCEPT_H1 confirmation; no parallel
  formal run of the same experiment.

The plan is a PURE READ: zero DB mutation. The wizard's confirm step
re-runs the whole planner and compares the canonical ``plan_digest``;
only an identical digest may create the DRAFT tournament (through the
existing ``create_tournament``).
"""

from __future__ import annotations

import hashlib
import json
import secrets

from ..models import (
    SPRT_ACCEPT_H1,
    EnginePreset,
    OpeningSet,
    Tournament,
)
from .openings import (
    opening_fens_for_indices,
    resolve_opening_plies,
    select_opening_indices,
    verify_prior_opening_snapshot,
)
from .sprt import wald_bounds
from .versions import (
    get_channel,
    get_version,
    identity_fingerprint,
    validate_version_build_provenance,
)

BASELINE_CHANNEL = "current-final"

# Active (non-terminal) statuses that block a parallel formal run.
_ACTIVE_STATUSES = ("DRAFT", "QUEUED", "RUNNING", "PAUSING", "PAUSED")

FORMAL_PROTOCOL_SCHEMA_VERSION = 1


class FormalExperimentPlan(dict):
    @property
    def ok(self) -> bool:
        return not self["errors"]


def _sha256_json(obj) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(",", ":"))
        .encode("utf-8")
    ).hexdigest()


def _resolve_candidate(session, candidate_ref: str) -> tuple[dict, list]:
    """Resolve 'preset:<id>' or 'version:<id>' into a canonical launch
    identity dict. Returns (identity, errors)."""
    errors: list[str] = []
    kind, _, ref = candidate_ref.partition(":")
    if kind == "preset":
        preset = (
            session.query(EnginePreset)
            .filter(EnginePreset.preset_id == ref)
            .first()
        )
        if preset is None or not preset.enabled:
            return {}, [f"unknown or disabled candidate preset {ref}"]
        from ..models import EngineBuild

        build = (
            session.query(EngineBuild)
            .filter(EngineBuild.build_id == preset.build_id)
            .first()
        )
        if build is None or not build.enabled:
            return {}, [f"candidate preset build disabled: {preset.build_id}"]
        args = list(preset.command_args or [])
        opts = dict(preset.uci_options or {})
        return {
            "kind": "preset",
            "ref": preset.preset_id,
            "display_name": preset.display_name,
            "build_id": build.build_id,
            "command_args": args,
            "uci_options": opts,
            "source_sha": build.git_sha,
            "binary_sha256": build.binary_sha256,
            "fingerprint": identity_fingerprint(
                build.binary_sha256, args, opts),
        }, errors
    if kind == "version":
        version = get_version(session, ref)
        if version is None:
            return {}, [f"unknown candidate version {ref}"]
        if version.status == "historical":
            return {}, [
                f"candidate version {ref} is historical and cannot be a "
                f"formal candidate"
            ]
        # A formal confirmation candidate that is an EngineVersion must be
        # pre-production (candidate|experimental). Production versions are
        # not formal candidates (the baseline IS the production); the UI
        # picker is not the security boundary.
        if version.status not in ("candidate", "experimental"):
            return {}, [
                f"candidate version {ref} is {version.status}; formal "
                f"candidates must be candidate or experimental versions "
                f"(or experimental presets)"
            ]
        # Re-validate the version against the CURRENT build registry
        # (shared gate with promotion — never trust the version row alone).
        provenance = validate_version_build_provenance(
            session, version, label="candidate version")
        if provenance:
            return {}, provenance
        return {
            "kind": "version",
            "ref": version.version_id,
            "display_name": version.display_name,
            "build_id": version.build_id,
            "command_args": list(version.command_args or []),
            "uci_options": dict(version.uci_options or {}),
            "source_sha": version.source_sha,
            "binary_sha256": version.binary_sha256,
            "fingerprint": version.identity_fingerprint,
        }, errors
    return {}, [
        f"candidate must be 'preset:<id>' or 'version:<id>', got "
        f"{candidate_ref!r}"
    ]


def _resolve_baseline(session) -> tuple[dict, list]:
    """The formal baseline is ALWAYS the current production EngineVersion
    behind channel:current-final. There is no user input."""
    errors: list[str] = []
    channel = get_channel(session, BASELINE_CHANNEL)
    if channel is None:
        return {}, [f"channel {BASELINE_CHANNEL} does not exist"]
    version = get_version(session, channel.engine_version_id)
    if version is None:
        return {}, [
            f"channel {BASELINE_CHANNEL} points at unknown version "
            f"{channel.engine_version_id}"
        ]
    if version.status != "production":
        return {}, [
            f"channel {BASELINE_CHANNEL} target {version.version_id} is "
            f"not production"
        ]
    # The formal baseline must be runtime-ready against the CURRENT
    # registry (same shared gate as promotion/candidates): a disabled or
    # drifted production build blocks formal experiment creation.
    provenance = validate_version_build_provenance(
        session, version, label="baseline version")
    if provenance:
        return {}, provenance
    return {
        "kind": "version",
        "ref": version.version_id,
        "display_name": version.display_name,
        "build_id": version.build_id,
        "command_args": list(version.command_args or []),
        "uci_options": dict(version.uci_options or {}),
        "source_sha": version.source_sha,
        "binary_sha256": version.binary_sha256,
        "fingerprint": version.identity_fingerprint,
    }, errors


def _prior_runs_for_experiment(session, experiment_id: str) -> list:
    """Every prior tournament whose frozen envelope carries this
    experiment_id — both automatic discovery and gate checks use it."""
    rows = session.query(Tournament).all()
    out = []
    for t in rows:
        env = (t.config_snapshot or {}).get("experiment") or {}
        if env.get("experiment_id") == experiment_id:
            out.append(t)
    return out


def _prior_opening_fens(session, prior: Tournament) -> list[str]:
    """Rebuild a prior run's frozen opening sample as canonical FENs.

    Fail-closed: the snapshot must carry an opening identity, the
    registry row must resolve, and BOTH the registry SHA and the actual
    file on disk must still match the frozen snapshot SHA. Uses the
    prior's FULL planned indices (not just completed pairs): a cancelled
    run's pre-assigned openings must not come back either.
    """
    snap = prior.config_snapshot or {}
    osnap = snap.get("opening_set") or {}
    indices = osnap.get("indices") or []
    if not indices:
        raise ValueError(
            f"prior tournament {prior.id} ({prior.name}) has no frozen "
            f"opening indices"
        )
    opening_set = (
        session.query(OpeningSet)
        .filter(OpeningSet.opening_set_id == osnap.get("opening_set_id"))
        .first()
    )
    if opening_set is None:
        raise ValueError(
            f"prior tournament {prior.id} references unknown opening set "
            f"{osnap.get('opening_set_id')}"
        )
    verify_prior_opening_snapshot(opening_set, osnap)
    return opening_fens_for_indices(
        opening_set, indices, osnap.get("plies")
    )


def plan_formal_experiment(
    session,
    draft,
    opening_set: OpeningSet,
    seed: int | None = None,
) -> FormalExperimentPlan:
    """Build the complete formal experiment plan. PURE READ.

    ``draft`` is a FormalExperimentDraft; ``opening_set`` the resolved
    registry row for draft.opening_set_id; ``seed`` the frozen seed (the
    wizard generates one at preview time when the draft leaves it empty).
    """
    errors: list[str] = []
    warnings: list[str] = []

    # --- opening depth: the ONE shared resolution contract ----------------
    # (PGN: requested or manifest default; EPD: must be None). The resolved
    # value is what preview displays, what the digest pins and what the
    # created snapshot freezes — identical by construction.
    try:
        resolved_plies = resolve_opening_plies(opening_set,
                                               draft.opening_plies)
    except Exception as exc:  # noqa: BLE001 — surfaced as plan errors
        return FormalExperimentPlan(
            ok=False,
            errors=[f"opening plies contract violated: {exc}"],
            warnings=[],
            experiment={
                "experiment_id": draft.experiment_id,
                "purpose": draft.purpose,
                "stage": draft.stage,
                "decision_rule": "sprt",
            },
            candidate=None,
            baseline=None,
            sprt={},
            opening={},
            automatic_prior_tournament_ids=[],
            explicit_prior_tournament_ids=[],
            excluded_fens_count=0,
            excluded_fens_sha256="",
            plan_digest="",
        )

    # --- baseline (always current-final) -------------------------------
    baseline, baseline_errors = _resolve_baseline(session)
    errors.extend(baseline_errors)

    # --- candidate ------------------------------------------------------
    candidate, candidate_errors = _resolve_candidate(session, draft.candidate)
    errors.extend(candidate_errors)

    # --- promotion-specific candidate gate ------------------------------
    if draft.stage == "promotion" and candidate:
        if candidate["kind"] != "version":
            errors.append(
                "promotion candidate must be an EngineVersion "
                "(default-identity artifact); experimental presets go "
                "through confirmation first"
            )
        else:
            from .versions import plan_channel_promotion

            promo = plan_channel_promotion(
                session, BASELINE_CHANNEL, candidate["ref"]
            )
            if not promo.ok:
                errors.extend(
                    f"promotion gate: {e}" for e in promo["errors"])

    # --- candidate vs baseline sanity ------------------------------------
    if candidate and baseline and not errors:
        if candidate["fingerprint"] == baseline["fingerprint"]:
            errors.append(
                "candidate and baseline resolve to the SAME launch "
                "identity — a formal experiment against itself is invalid"
            )

    # --- SPRT contract (model fixed; parameters validated) --------------
    if draft.elo0 >= draft.elo1:
        errors.append(f"elo0 must be < elo1 (got {draft.elo0} >= {draft.elo1})")
    lower, upper = wald_bounds(draft.alpha, draft.beta)
    sprt_contract = {
        "enabled": True,
        "unit": "pair",
        "model": "pentanomial",
        "elo_model": "logistic",
        "elo0": draft.elo0,
        "elo1": draft.elo1,
        "alpha": draft.alpha,
        "beta": draft.beta,
        "lower_bound": lower,
        "upper_bound": upper,
        "max_pairs": draft.max_pairs,
    }

    # --- same-experiment prior runs: discovery + stage gates ------------
    priors = _prior_runs_for_experiment(session, draft.experiment_id)
    automatic_priors = [t.id for t in priors]
    active_priors = [
        t for t in priors if t.status in _ACTIVE_STATUSES
    ]
    if active_priors:
        errors.append(
            f"experiment {draft.experiment_id} already has an active "
            f"formal run ({', '.join(t.id[:8] for t in active_priors)}); "
            f"parallel formal runs would pollute each other's opening "
            f"sample and sequential interpretation"
        )
    if draft.stage == "confirmation":
        terminal_confirmations = [
            t for t in priors
            if t.status in (SPRT_ACCEPT_H1, "SPRT_ACCEPT_H0",
                            "SPRT_MAX_PAIRS")
            and ((t.config_snapshot or {}).get("experiment") or {})
            .get("stage") == "confirmation"
            and ((t.config_snapshot or {}).get("experiment") or {})
            .get("decision_rule") == "sprt"
        ]
        if terminal_confirmations:
            errors.append(
                f"experiment {draft.experiment_id} already has a "
                f"completed formal confirmation "
                f"({', '.join(t.id[:8] for t in terminal_confirmations)}); "
                f"do not reopen a terminated sequential test — use a new "
                f"experiment_id or a future replication workflow"
            )
    if draft.stage == "promotion":
        accepted_confirmations = [
            t for t in priors
            if t.status == SPRT_ACCEPT_H1
            and ((t.config_snapshot or {}).get("experiment") or {})
            .get("stage") == "confirmation"
            and ((t.config_snapshot or {}).get("experiment") or {})
            .get("decision_rule") == "sprt"
        ]
        if not accepted_confirmations:
            errors.append(
                f"promotion stage requires a prior "
                f"stage=confirmation ACCEPT_H1 run for experiment "
                f"{draft.experiment_id}; none found"
            )

    # --- opening independence -------------------------------------------
    explicit_priors: list[str] = []
    prior_sources: list[Tournament] = []
    for tid in draft.explicit_prior_tournament_ids:
        t = session.get(Tournament, tid)
        if t is None:
            errors.append(f"explicit prior tournament not found: {tid}")
            continue
        explicit_priors.append(tid)
        prior_sources.append(t)
    prior_sources.extend(priors)

    excluded_fens: set[str] = set()
    exclusion_failures: list[str] = []
    for t in prior_sources:
        try:
            excluded_fens.update(_prior_opening_fens(session, t))
        except Exception as exc:  # noqa: BLE001 — surfaced as plan errors
            exclusion_failures.append(str(exc))
    errors.extend(exclusion_failures)

    excluded_fens_list = sorted(excluded_fens)
    excluded_fens_sha256 = _sha256_json(excluded_fens_list)

    # --- opening set identity (the NEW sample source) --------------------
    from .openings import sha256_file
    from pathlib import Path

    opening_path = Path(opening_set.file_path)
    if not opening_path.is_file():
        errors.append(f"opening set file missing: {opening_path}")
        file_sha = None
    else:
        file_sha = sha256_file(opening_path)
        if opening_set.sha256 and file_sha != opening_set.sha256:
            errors.append(
                f"opening file on disk does not match the registered "
                f"OpeningSet sha256"
            )

    # --- deterministic sample --------------------------------------------
    selected_indices: list[int] = []
    eligible_before = 0
    eligible_after = 0
    if not errors:
        from .openings import eligible_openings

        try:
            pool_all = eligible_openings(opening_set, resolved_plies)
            eligible_before = len(pool_all)
            chosen_seed = seed if seed is not None else 0
            selected_indices = select_opening_indices(
                opening_set,
                draft.max_pairs,
                resolved_plies,
                chosen_seed,
                exclude_fens=excluded_fens_list,
            )
            # recompute the post-exclusion pool size for the preview
            from .openings import _eligible_fens_by_index

            fens_by_index = _eligible_fens_by_index(
                opening_set, resolved_plies, pool_all)
            pool_after = [
                i for i in pool_all
                if fens_by_index.get(i) not in set(excluded_fens_list)
            ]
            eligible_after = len(pool_after)
            if eligible_after < draft.max_pairs:
                errors.append(
                    f"opening book has only {eligible_after} eligible "
                    f"openings after exclusion; need {draft.max_pairs} "
                    f"(before exclusion: {eligible_before})"
                )
                selected_indices = []
        except Exception as exc:  # noqa: BLE001 — surfaced as plan errors
            errors.append(f"opening selection failed: {exc}")

    selected_indices_sha256 = (
        _sha256_json(selected_indices) if selected_indices else None
    )

    plan = FormalExperimentPlan(
        ok=not errors,
        errors=errors,
        warnings=warnings,
        experiment={
            "experiment_id": draft.experiment_id,
            "purpose": draft.purpose,
            "stage": draft.stage,
            "decision_rule": "sprt",
        },
        candidate=candidate,
        baseline=baseline,
        sprt=sprt_contract,
        opening={
            "opening_set_id": opening_set.opening_set_id,
            "file_sha256": file_sha,
            "plies": resolved_plies,
            "seed": seed,
            "eligible_before": eligible_before,
            "eligible_after": eligible_after,
            "selected_count": len(selected_indices),
            "selected_indices": selected_indices[:10],
            "selected_indices_sha256": selected_indices_sha256,
        },
        automatic_prior_tournament_ids=automatic_priors,
        explicit_prior_tournament_ids=explicit_priors,
        excluded_fens_count=len(excluded_fens_list),
        excluded_fens_sha256=excluded_fens_sha256,
    )

    # --- canonical plan digest -------------------------------------------
    digest_payload = {
        "experiment": plan["experiment"],
        "candidate": {k: candidate.get(k) for k in (
            "kind", "ref", "build_id", "source_sha", "command_args",
            "uci_options", "binary_sha256", "fingerprint")}
        if candidate else None,
        "baseline": {k: baseline.get(k) for k in (
            "kind", "ref", "build_id", "source_sha", "command_args",
            "uci_options", "binary_sha256", "fingerprint")}
        if baseline else None,
        "sprt": sprt_contract,
        "opening_set_id": opening_set.opening_set_id,
        "opening_file_sha256": file_sha,
        "opening_plies": resolved_plies,
        "opening_seed": seed,
        "automatic_prior_tournament_ids": sorted(automatic_priors),
        "explicit_prior_tournament_ids": sorted(explicit_priors),
        "excluded_fens_sha256": excluded_fens_sha256,
        "selected_indices_sha256": selected_indices_sha256,
    }
    plan["plan_digest"] = _sha256_json(digest_payload)
    return plan


def generate_opening_seed() -> int:
    """A fresh 31-bit seed, generated ONCE at preview time."""
    return secrets.randbelow(2 ** 31)
