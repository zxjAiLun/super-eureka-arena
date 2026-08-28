"""V2.2-A: experiment read model — organize existing frozen facts.

An "experiment view" is derived ENTIRELY from the tournament's frozen
``config_snapshot`` plus its live pair results:

- candidate/baseline identities come from the frozen engine_a/engine_b
  snapshots (never re-resolved from live presets/versions — that would
  break experiment reproducibility);
- the SPRT state comes from the shared ``sprt.tournament_sprt_state``
  read model (the same code the scheduler uses);
- the run state is mapped from the tournament status.

Nothing here mutates the database.
"""

from __future__ import annotations

from .sprt import tournament_sprt_state

# Frozen-envelope schema written into config_snapshot["experiment"].
EXPERIMENT_SCHEMA_VERSION = 1

# Run state -> experiment state mapping (tournament status is the source
# of truth; SPRT terminal statuses translate to decision-flavored states).
_Sprt_TERMINAL_MAP = {
    "SPRT_ACCEPT_H1": "accepted_h1",
    "SPRT_ACCEPT_H0": "accepted_h0",
    "SPRT_MAX_PAIRS": "max_pairs",
}

_EXPERIMENT_STATE_MAP = {
    "DRAFT": "draft",
    "QUEUED": "queued",
    "RUNNING": "running",
    "PAUSING": "pausing",
    "PAUSED": "paused",
    "COMPLETED": "completed",
    "CANCELLED": "cancelled",
    "FORCE_CANCELLED": "cancelled",
    "INTERRUPTED": "failed",
    "FAILED": "failed",
}

# Interpretation copy for terminal SPRT decisions. Deliberately precise:
# ACCEPT_H0 means the run fell to the H0 boundary UNDER THIS SPRT
# CONFIGURATION, not a mathematical proof of no improvement.
_DECISION_INTERPRETATION = {
    "ACCEPT_H1": (
        "Evidence crossed the configured H1 (elo1) boundary."
    ),
    "ACCEPT_H0": (
        "The run fell to the H0 boundary under this SPRT configuration — "
        "the candidate did not clear the configured hypothesis; this is "
        "not a mathematical proof of no improvement."
    ),
    "MAX_PAIRS": (
        "Maximum pair budget reached without crossing either boundary."
    ),
    "CONTINUE": "Sequential test still in progress.",
}


def experiment_view(tournament) -> dict | None:
    """The renderable experiment view of a tournament, or None when its
    frozen snapshot carries no experiment envelope (legacy matches)."""
    snap = tournament.config_snapshot or {}
    env = snap.get("experiment")
    if not env:
        return None

    state = _EXPERIMENT_STATE_MAP.get(tournament.status, "unknown")
    sprt_state = tournament_sprt_state(tournament)
    if tournament.status in _Sprt_TERMINAL_MAP:
        state = _Sprt_TERMINAL_MAP[tournament.status]

    view = {
        "experiment_id": env.get("experiment_id"),
        "purpose": env.get("purpose"),
        "stage": env.get("stage"),
        # Identities come ONLY from the frozen snapshot.
        "candidate": snap.get("engine_a") or {},
        "baseline": snap.get("engine_b") or {},
        "decision_rule": env.get("decision_rule") or "fixed_pairs",
        "sprt": sprt_state,
        "state": state,
        # completed/total pairs for progress rendering
        "pairs_completed": tournament.completed_pairs or 0,
        "pairs_total": (
            tournament.requested_pairs
            if sprt_state is None
            else sprt_state.get("max_pairs")
        ),
        "candidate_wins": tournament.candidate_wins or 0,
        "candidate_losses": tournament.candidate_losses or 0,
        "draws": tournament.draws or 0,
    }
    if sprt_state is not None:
        view["decision_interpretation"] = _DECISION_INTERPRETATION.get(
            sprt_state.get("decision", ""), "")
    return view


def side_display_name(side: dict) -> str:
    """A human label for a frozen snapshot side, from the frozen data
    only."""
    if not side:
        return "unknown"
    if side.get("version_id"):
        base = side.get("display_name") or side["version_id"]
        return f"{base} ({side['version_id']})"
    return side.get("display_name") or side.get("preset_id") or "unknown"
