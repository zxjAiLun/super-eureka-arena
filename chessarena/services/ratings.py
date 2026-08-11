"""Arena Elo v2 (P4.12 commit 2): standard participant Elo recomputed from
verified history.

Design:

- Identity is the frozen engine configuration (binary_sha256 + command_args +
  uci_options hashed) — never the display name, so different builds/profiles
  never merge even when names are identical.
- Every public/enabled participant is shown even with zero games: ordinary
  engines start at 1800 (status "initial").
- Stockfish Limited anchors (UCI_LimitStrength + UCI_Elo on a registered
  Stockfish build) are FIXED at their UCI_Elo and never update.
- Rated history is only: arena_elo_enabled + result-terminal tournaments
  (full schedule or early SPRT decision) + VERIFIED games.  Engine-vs-engine
  matches update BOTH sides; engine-vs-anchor updates only the engine.
- Per game, standard Elo with K=16:

      Ea = 1 / (1 + 10 ** ((Rb - Ra) / 400))
      Ra' = Ra + K * (Sa - Ea)        (Sa: win 1, draw 0.5, loss 0)

- Play order is deterministic per time-control pool:
  (finished_at, created_at, tournament_id, game_number).
- Recomputed on every request; deleting a match removes its games on the
  next recompute.
"""

from __future__ import annotations

import hashlib
import json
from typing import Optional

from ..config import TIME_CONTROLS
from ..models import (
    RESULT_TERMINAL_STATUSES,
    EngineBuild,
    EnginePreset,
    EngineVersion,
    Game,
    Tournament,
    coerce_utc,
)

INITIAL_RATING = 1800.0
K_FACTOR = 16
ANCHOR_ENGINE_NAME = "Stockfish"


def engine_fingerprint(side: dict) -> str:
    """A competitor is one frozen configuration: binary + command args +
    UCI options."""
    payload = {
        "binary_sha256": (side or {}).get("binary_sha256"),
        "command_args": (side or {}).get("command_args") or [],
        "uci_options": (side or {}).get("uci_options") or {},
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def resolve_participant_id(session, side: dict) -> str:
    """S4.3E ADR: the rating participant identity for one frozen side.

    Resolution rule:
    - frozen snapshot has version_id           -> that version_id
    - else legacy fingerprint matching exactly
      one EngineVersion.identity_fingerprint   -> that EngineVersion.version_id
    - else                                     -> "legacy:" + fingerprint
    """
    vid = (side or {}).get("version_id")
    if vid:
        return vid
    fp = engine_fingerprint(side)
    matches = (
        session.query(EngineVersion)
        .filter(EngineVersion.identity_fingerprint == fp)
        .all()
    )
    if len(matches) == 1:
        return matches[0].version_id
    return "legacy:" + fp


def _uci_elo(side: dict) -> Optional[int]:
    opts = (side or {}).get("uci_options") or {}
    limit = opts.get("UCI_LimitStrength")
    elo = opts.get("UCI_Elo")
    if limit is not True and str(limit).lower() != "true":
        return None
    try:
        return int(elo)
    except (TypeError, ValueError):
        return None


def _is_stockfish_build(session, side: dict) -> bool:
    build_id = (side or {}).get("build_id")
    if not build_id:
        return False
    build = (
        session.query(EngineBuild)
        .filter(EngineBuild.build_id == build_id)
        .first()
    )
    # Fail closed: only an actual registered Stockfish build is an anchor.  A
    # missing build row must not be guessed to be Stockfish (some other engine
    # with UCI_LimitStrength/UCI_Elo could then be misread as a fixed anchor).
    return build is not None and build.engine_name == ANCHOR_ENGINE_NAME


def is_anchor(session, side: dict) -> bool:
    return _uci_elo(side) is not None and _is_stockfish_build(session, side)


def anchor_rating(side: dict) -> Optional[int]:
    return _uci_elo(side)


def _side_from_preset(build: EngineBuild, preset: EnginePreset) -> dict:
    return {
        "preset_id": preset.preset_id,
        "display_name": preset.display_name,
        "build_id": build.build_id,
        "command_args": list(preset.command_args or []),
        "uci_options": dict(preset.uci_options or {}),
        "binary_sha256": build.binary_sha256,
    }


def _score_for_a(game: Game) -> Optional[float]:
    """Game result from Engine A's perspective (pair color contract: odd
    game_number -> A is White)."""
    a_white = game.game_number % 2 == 1
    if game.result == "1-0":
        return 1.0 if a_white else 0.0
    if game.result == "0-1":
        return 0.0 if a_white else 1.0
    if game.result == "1/2-1/2":
        return 0.5
    return None


def _history_side(side: dict) -> dict:
    """Participant metadata for a snapshot side that is not a public participant
    (archived/legacy configs that still have rated history)."""
    return {
        "display_name": (side.get("display_name")
                         or side.get("preset_id") or "unknown"),
        "is_anchor": False,
        "anchor_rating": None,
    }


def _participant_base(session) -> dict:
    """participant_id -> base metadata for every public/enabled participant.

    Ordinary rated participants come from EngineVersion (version_id is the
    Elo identity); Stockfish Limited anchors stay on the existing preset path
    (fixed UCI_Elo semantics). Ordinary presets are no longer auto-participants.
    """
    out: dict[str, dict] = {}
    versions = (
        session.query(EngineVersion)
        .filter(
            EngineVersion.public_visible.is_(True),
            EngineVersion.rating_enabled.is_(True),
        )
        .all()
    )
    for version in versions:
        out[version.version_id] = {
            "display_name": version.display_name,
            "is_anchor": False,
            "anchor_rating": None,
            "fingerprint": version.identity_fingerprint,
        }
    presets = (
        session.query(EnginePreset)
        .filter(
            EnginePreset.public_visible.is_(True),
            EnginePreset.enabled.is_(True),
        )
        .all()
    )
    for preset in presets:
        build = (
            session.query(EngineBuild)
            .filter(EngineBuild.build_id == preset.build_id)
            .first()
        )
        if build is None or not build.enabled:
            continue
        side = _side_from_preset(build, preset)
        if not is_anchor(session, side):
            continue
        fp = engine_fingerprint(side)
        out[fp] = {
            "display_name": preset.display_name,
            "is_anchor": True,
            "anchor_rating": anchor_rating(side),
            "fingerprint": fp,
        }
    return out


def compute_ratings(session) -> dict:
    """{time_control: {"engines": [rows incl. anchors], "anchors": [...]}}.

    Each engine row: participant_id, fingerprint, display_name, rating (int),
    games, wins, draws, losses, status (fixed | initial | rated).
    Deterministic. Participants are keyed by the S4.3E ADR participant
    identity (version_id, fingerprint for anchors, or "legacy:" + fingerprint
    for unmapped history).

    Public participants appear in EVERY pool (even with zero games);
    history-only identities (archived/legacy configs) appear ONLY in the pools
    where they actually have rated history.
    """
    public_participants = _participant_base(session)
    history_by_tc: dict[str, dict[str, dict]] = {
        tc: {} for tc in TIME_CONTROLS
    }
    anchor_by_tc: dict[str, dict[str, dict]] = {
        tc: {} for tc in TIME_CONTROLS
    }
    pools: dict[str, list] = {tc: [] for tc in TIME_CONTROLS}

    matches = (
        session.query(Tournament)
        .filter(
            Tournament.status.in_(RESULT_TERMINAL_STATUSES),
            Tournament.arena_elo_enabled.is_(True),
        )
        .all()
    )
    for t in matches:
        tc = t.time_control
        if tc not in pools:
            continue
        snap = t.config_snapshot or {}
        side_a, side_b = snap.get("engine_a") or {}, snap.get("engine_b") or {}
        pid_a = resolve_participant_id(session, side_a)
        pid_b = resolve_participant_id(session, side_b)
        fp_a = engine_fingerprint(side_a)
        fp_b = engine_fingerprint(side_b)
        history_by_tc[tc].setdefault(
            pid_a, {**_history_side(side_a), "fingerprint": fp_a}
        )
        history_by_tc[tc].setdefault(
            pid_b, {**_history_side(side_b), "fingerprint": fp_b}
        )
        anchor_a = is_anchor(session, side_a)
        anchor_b = is_anchor(session, side_b)
        if anchor_a:
            anchor_by_tc[tc][pid_a] = {
                "display_name": side_a.get("display_name")
                or side_a.get("preset_id") or "unknown",
                "is_anchor": True,
                "anchor_rating": anchor_rating(side_a),
                "fingerprint": fp_a,
            }
        if anchor_b:
            anchor_by_tc[tc][pid_b] = {
                "display_name": side_b.get("display_name")
                or side_b.get("preset_id") or "unknown",
                "is_anchor": True,
                "anchor_rating": anchor_rating(side_b),
                "fingerprint": fp_b,
            }
        games = (
            session.query(Game)
            .filter(Game.tournament_id == t.id, Game.verified.is_(True))
            .order_by(Game.game_number)
            .all()
        )
        for g in games:
            sa = _score_for_a(g)
            if sa is None:
                continue
            order = (
                coerce_utc(t.finished_at) or coerce_utc(t.created_at),
                coerce_utc(t.created_at),
                t.id,
                g.game_number,
            )
            pools[tc].append((order, pid_a, pid_b, sa))

    result: dict[str, dict] = {}
    for tc, pool_games in pools.items():
        merged: dict[str, dict] = {}
        for pid, meta in public_participants.items():
            merged[pid] = meta
        # History-only identities join only the pools they played in.
        for pid, meta in history_by_tc[tc].items():
            merged.setdefault(pid, meta)
        # Snapshot anchors override whatever the base metadata said.
        for pid, meta in anchor_by_tc[tc].items():
            merged[pid] = meta

        rows: dict[str, dict] = {}
        for pid, base in merged.items():
            anchor_elo = base.get("anchor_rating")
            rows[pid] = {
                "participant_id": pid,
                "fingerprint": base.get("fingerprint") or pid,
                "display_name": base["display_name"],
                "rating": (
                    float(anchor_elo)
                    if base["is_anchor"] and anchor_elo is not None
                    else INITIAL_RATING
                ),
                "is_anchor": base["is_anchor"],
                "games": 0,
                "wins": 0,
                "draws": 0,
                "losses": 0,
            }
        for _, pid_a, pid_b, sa in sorted(pool_games):
            # Same-identity self-play carries no relative-strength
            # information (and would double-count Games/W-D-L), so the game
            # never enters the rating statistics.
            if pid_a == pid_b:
                continue
            a = rows.get(pid_a)
            b = rows.get(pid_b)
            if a is None or b is None:
                continue
            ea = 1 / (1 + 10 ** ((b["rating"] - a["rating"]) / 400))
            sb = 1 - sa
            if not a["is_anchor"]:
                a["rating"] += K_FACTOR * (sa - ea)
            if not b["is_anchor"]:
                b["rating"] += K_FACTOR * (sb - (1 - ea))
            a["games"] += 1
            b["games"] += 1
            if sa == 1.0:
                a["wins"] += 1
            elif sa == 0.5:
                a["draws"] += 1
            else:
                a["losses"] += 1
            if sb == 1.0:
                b["wins"] += 1
            elif sb == 0.5:
                b["draws"] += 1
            else:
                b["losses"] += 1

        engines = []
        for pid, r in sorted(rows.items(),
                            key=lambda kv: (kv[1]["display_name"].lower(), kv[0])):
            engines.append(
                {
                    "participant_id": pid,
                    "fingerprint": r["fingerprint"],
                    "display_name": r["display_name"],
                    "rating": int(round(r["rating"])),
                    "games": r["games"],
                    "wins": r["wins"],
                    "draws": r["draws"],
                    "losses": r["losses"],
                    "status": (
                        "fixed" if r["is_anchor"]
                        else ("rated" if r["games"] else "initial")
                    ),
                }
            )
        anchors = sorted(
            (
                {"rating": int(round(r["rating"])),
                 "display_name": r["display_name"]}
                for r in rows.values()
                if r["is_anchor"]
            ),
            key=lambda a: a["rating"],
        )
        result[tc] = {"engines": engines, "anchors": anchors}
    return result


def engine_rating(session, t: Tournament) -> Optional[dict]:
    """Current Arena Elo row of the match's candidate side (the non-anchor
    side when exactly one side is a fixed anchor, else engine A), or None
    when the match does not participate."""
    if t.status not in RESULT_TERMINAL_STATUSES or not t.arena_elo_enabled:
        return None
    snap = t.config_snapshot or {}
    side_a, side_b = snap.get("engine_a") or {}, snap.get("engine_b") or {}
    a_anchor = is_anchor(session, side_a)
    b_anchor = is_anchor(session, side_b)
    target = side_b if a_anchor and not b_anchor else side_a
    participant_id = resolve_participant_id(session, target)
    all_ratings = compute_ratings(session)
    for row in all_ratings.get(t.time_control, {}).get("engines", []):
        if row.get("participant_id") == participant_id:
            return row
    return None
