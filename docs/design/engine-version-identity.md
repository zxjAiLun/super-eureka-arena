# Design: EngineVersion identity model (frozen 2026-08-10)

Status: **DESIGN DECISION — frozen, not yet implemented**. Schema/migration work
is deferred until after the S4.3D formal SPRT conclusion and the S4.3E
promotion (do NOT implement during an active formal test).

## Problem

Arena currently binds "how an engine is launched" and "long-term Elo identity"
together through `--profile` names on a single binary. That is ideal for
isolated A/B experiments (same ELF, same compiler/linker, only the target code
path differs) but it is NOT a durable identity model:

- historical versions would accumulate as `--profile 0810X`, `--profile 0820Y`, ...;
- the same binary carries multiple playable "players" but Elo must not be bound
  to the binary (build) or to a mutable launch template (preset).

## Model (one-line contract)

> **`EngineVersion.version_id` IS the rating participant identity.**
> `EnginePreset` only provides a launch SNAPSHOT at version-creation time;
> it is not the long-term Elo identity. Do not split a separate
> `RatingParticipant` table until a real "one version, many identities" need
> appears.

```text
EngineBuild            physical immutable artifact (the ELF)
EnginePreset           dev/experimental/operational launch template (mutable-ish)
EngineVersion          permanent immutable chess identity == Elo participant
EngineChannel          mutable alias (e.g. CurrentFinal) -> points to an EngineVersion
RatingPool             rating environment contract (TC / Threads / Hash / opening policy)
Rating                 long-term Elo under (version_id, pool_id)
```

## Layer semantics

### 1. EngineBuild — physical artifact

- id: `20260809-b4de653-linux-x86_64`
- binds: binary sha256, source sha, rustc, Cargo.lock sha, manifest, supported profiles
- answers: "which ELF actually runs?"
- already implemented; unchanged.

### 2. EnginePreset — how to launch the artifact

- e.g. `s43b-current-final` = build + `["--profile","current-final"]`;
  `s43b-legality-fast` = build + `["--profile","current-final-legality-fast"]`
- answers: "with what arguments does this ELF start?"
- dev/experimental/management layer. Presets may be updated operationally.

### 3. EngineVersion — permanent immutable chess identity (== participant)

Fields (frozen at creation, never mutated):

```text
version_id
display_name

build_id
command_args
uci_options

source_sha
binary_sha256

created_at
status          candidate | production | historical | experimental
rating_enabled
public_visible
```

- **Immutability contract**: at creation, SNAPSHOT
  `preset.command_args`, `preset.uci_options`, `preset.build_id` into the
  version. Later edits to any preset must NEVER affect an existing version —
  otherwise historical Elo identity drifts.
- **Promotion does not rewrite history**: a promoted version is a NEW immutable
  version; the old version (rating, artifact, matches) stays forever and can
  keep playing in the pool ("关公战秦琼").

### 4. EngineChannel — mutable alias

- e.g. `channel_id: current-final` -> `engine_version_id: ce-currentfinal-20260811`
- promotion = repoint the channel `old -> new`; the old version is untouched.

### 5. RatingPool + Rating

- pool contract: `pool: STC-10s-1T-16MB`, `pool: LTC-60s-1T-64MB` (TC /
  Threads / Hash / opening policy).
- Rating rows:

```text
Rating
  version_id
  pool_id
  rating
  games
  wins
  draws
  losses
```

- The same EngineVersion may hold separate ratings in different pools
  (no participant duplication needed for thread/hash differences — that is a
  pool contract, not an identity).

## `--profile` scope narrowing

- `--profile` stays for SAME-source-tree experiment isolation (A/B gold
  standard: same binary, only the target code path differs).
- After promotion, new production artifacts should run their DEFAULT behavior:
  `command_args = []`. Release versions = "artifact default behavior == that
  historical version itself", so replaying the version line is stable years
  later. Experimental artifacts keep their profiles as provenance.

## Lifecycle

```text
dev candidate
  -> same-binary profile A/B
  -> screen
  -> SPRT
  -> promotion
  -> new immutable production artifact (default = production behavior)
  -> new permanent EngineVersion (status=production)
  -> join Elo pool
  -> EngineChannel repoint
```

Rejected/experimental candidates (e.g. RootHistory, RootPrevScore) may be
registered as `status=experimental, rating_enabled=true` participants so their
true relative strength against the production line can be measured later.

## V2.1: controlled lifecycle + atomic promotion (frozen 2026-08-28)

### Immutability vs lifecycle metadata

"Immutable" covers exactly the chess/launch identity —
`version_id, build_id, command_args, uci_options, source_sha,
binary_sha256, identity_fingerprint` can NEVER change after creation.
The lifecycle metadata `status, public_visible, rating_enabled` is mutable
but ONLY through the controlled promotion flow — never ad-hoc edits:

```text
candidate    (hidden, unrated)   --create defaults
   |
   | promote_channel(channel, version)   # ONE transaction, all-or-nothing
   v
production   (public, rated)     + old production -> historical
   |
   v
historical   (public, rated)     # still a valid history-only participant
```

A version's identity is WHO THE BINARY IS (source/binary sha + launch
config), never "which semantic promote-commit it corresponds to". When the
Engine repo promotes at commit X but the first reproducible artifact built
afterwards is commit Y, the EngineVersion honestly records Y; the timeline
below separately narrates the X promotion.

### Registration rules (anti-garbage contract)

- Only immutable binaries with **experiment or promotion significance**
  become EngineVersions. NOT every git commit.
- Create defaults: `status=candidate, public_visible=false,
  rating_enabled=false`. The known-past-production exception (registering
  history directly as `historical/public/rated`) requires explicit flags.
- Experimental LOO/ablation profiles stay as hidden `EnginePreset`s
  (`public_visible=false`); they snapshot into a version only when they
  become a genuine promotion candidate or long-term Elo participant.
- Production versions use the artifact's default launch identity:
  `command_args=[], uci_options={}`. NEVER `["--profile", ...]` — the
  fingerprint includes command_args, so an explicit alias would create a
  second, artificial identity for the same chess player.

### Atomic promotion (`services.versions.promote_channel`)

```text
read channel -> validate target
  old production.status  -> historical          \
  target.status          -> production           | ONE transaction;
  target.public_visible  -> true                 | any failure rolls back
  target.rating_enabled  -> true                 | EVERYTHING
  channel                -> target              /
```

`plan_channel_promotion` builds the same view with ZERO mutation (CLI
dry-run default). Informational impact counts (rated history, active
human games on the channel, active tournaments) never block promotion:
tournaments and HumanGames hold frozen snapshots, so promotion only
affects the NEXT creation through the channel.

### Operator workflow (admin CLI)

```text
python -m chessarena.admin engine-version create \
    --build 20260825-96d1a69-linux-x86_64 \
    --version ce-currentfinal-20260825 \
    --name "CurrentFinal · Integrated Positional · 2026-08-25"

python -m chessarena.admin engine-channel promote \
    current-final ce-currentfinal-20260825        # dry-run by default
python -m chessarena.admin engine-channel promote \
    current-final ce-currentfinal-20260825 --yes  # atomic commit
```

## Checkpoint timeline (CurrentFinal lineage, narrated 2026-08-28)

Engine-repo promote commits that shaped CurrentFinal, and which
EngineVersion artifacts exist in Arena:

```text
2026-08-06  51a629f  CurrentFinal baseline
            -> ce-currentfinal-20260806 (historical, public, rated)
               build 20260806-51a629f-linux-x86_64

2026-08-11  26604c4  promote legality fast path (S4.3E)
            -> ce-currentfinal-20260811 (historical since V2.1 promotion,
               public, rated)
               build 20260811-26604c4-linux-x86_64

2026-08-12  710400a  promote single-buffer movegen (S4.4E)     [no artifact kept]
2026-08-13  8eb9bd6  promote single-generation probe            [no artifact kept]
2026-08-16  33dc5e7  promote null-window LMR (S7.4A)            [no artifact kept]
2026-08-17  a719b57  promote single-evasion (S7.5A)             [no artifact kept]
            (history milestones only — NOT registered as EngineVersions:
             no verifiable immutable artifact survives, so no identity)

2026-08-24  b2c0efe  promote integrated positional eval (S8.0,
            SPRT ACCEPT_H1 +71.3 Elo, +109.3 Elo blitz validation)
            -> first reproducible artifact AFTER this promotion:
               build 20260825-96d1a69-linux-x86_64 (5 commits later,
               includes S9 Eval2Mask infrastructure)
            -> ce-currentfinal-20260825
               (production, public, rated; command_args=[], uci_options={})
               display_name: CurrentFinal · Integrated Positional · 2026-08-25
               source_sha: 96d1a69b2d884b3f78703d8c87c973dff9eb7830
               (its current-final profile self-reports
                "eval handcrafted-v1+integrated-positional", verified)

2026-08-25  75e6eea..f46749a  S9 Eval2 LOO experiment profiles
            -> NOT EngineVersions; hidden EnginePresets
               (category=s9-loo-eval2, public_visible=false, enabled=true)
               on build 20260825-96d1a69-linux-x86_64
```

Rule frozen with this timeline: the timeline may say "the S8 promotion
happened on 08-24"; the EngineVersion says exactly which binary it is.

## When to split RatingParticipant (do NOT pre-commit)

Only split `EngineVersion -> RatingParticipant` if a real need appears:

- the same version entered under different Hash/threads, each with its own Elo —
  prefer RatingPool contract over participant duplication;
- league entries ("Team A entry" vs "Team B entry" of the same version).

Not now. `EngineVersion = RatingParticipant` is the cleanest current design.

## Current status (2026-08-10)

- S4.3D formal pentanomial SPRT (`86835da4-...`, +10/+30, alpha=beta=0.05,
  max 2000 pairs) terminated **ACCEPT_H1** at 263 pairs: Ptnml
  [28,27,111,42,55], LLR 2.9756 > upper 2.9444, candidate 56.56%.
- Awaiting independent review of the final pentanomial/LLR and the stopping
  pair; then S4.3E promotion (LegalityFast -> CurrentFinal) with a NEW
  production artifact — which becomes the first formal `EngineVersion`
  (`ce-currentfinal-20260811`), followed by backfilling the pre-LegalityFast
  `CurrentFinal` as a historical version.
- Schema implementation deferred until after promotion; no Arena
  schema/migration work during the formal test or before the promotion commit.
