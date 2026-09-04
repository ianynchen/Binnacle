# Component: Lifecycle Engine

## Purpose

The invariant-bearing core (REQUIREMENTS FR-3/4/5; ARCHITECTURE I-1..I-4). Every
status, link, and transition in the record passes through here, and nowhere else.
If this component is correct, the record cannot lie.

## Owns

- Both state machines and every legal transition (table below).
- The authority rule (I-2) and the discard-permission rule (FR-3.3).
- The atomic composition of each lifecycle act over store primitives (I-1).
- Queue-item resolution semantics (execute / decline / dismiss / defer).

## Depends on

The store port (transactions + primitives). No LLM, no embedder, no clock policy
(the archival *sweep* decides who is stale; the engine just executes `archived`
transitions it is handed — mechanism stays outside judgment).

## The state machines

**short_term** — the full exit matrix (FR-3.2/3.3/4.5; the transition-matrix
test enumerates every cell):

| From | Legal exits |
|---|---|
| `current` | `promoted` (gate), `not_promoted` (gate), `superseded` (any actor, ST successor), `discarded` (FR-3.3 rule), `archived` (clock; blocked while open queue items exist) |
| `not_promoted` | `promoted` (gate, after re-recommendation — FR-4.5), `superseded` (a newer working decision may still supersede it), `discarded` (human), `archived` (clock, same open-item block) |
| `archived` | reactivated → restored prior status (recorded explicitly in the transition's `new_status`); `discarded` (human) |
| `promoted` / `superseded` / `discarded` | terminal |

Re-recommendation of an `archived` decision by ANY actor implicitly reactivates
it (both transitions, one transaction) — recommending only feeds the gate, so
ownership does not constrain it (FR-3.4).

**long_term**: `current → superseded` (only by a long-term successor — FR-5.2a).
Terminal: `superseded`. Never archived.

Pendingness (recommendations, suggested links/supersedes) lives ONLY in `queue`
rows — a pending item never changes any status (I-4).

## Acts (each = one store transaction, I-1)

| Act | Who may | Effect |
|---|---|---|
| record | any actor | decision row + `recorded` transition (+ immediate ST-target supersede links; LT-target claims → queue). Same-id retry: hash-identical no-op, divergent `IdempotencyConflict` |
| record_long_term | human | LT row + `recorded` + `promoted` transitions (FR-4.4) |
| recommend | any actor | queue item + `recommended` transition |
| promote (verbatim) | human | LT copy + `PROMOTED_FROM` link + pending-supersede execution + source→`promoted` + queue resolve + transitions |
| promote_refined | human | as promote, but LT row = human-authored content, ≥1 sources each linked+`promoted`; transitions carry `refined: true` (FR-4.6) |
| decline | human | source→`not_promoted` + queue resolve + transition with reason |
| discard | recorder-of-own-ST or human | →`discarded` + transition (FR-3.3) + auto-`voided` resolution of open queue items |
| supersede | actor; human AND long_term successor required if target is long_term (FR-5.2a) | link + target→`superseded` + transitions on both + auto-`voided` resolution of the target's open queue items |
| supplement | actor; human if target is long_term | link + `supplement_linked` transitions; NO status change (FR-5.3) |
| archive / reactivate | engine (sweep) / any-on-own, human-on-any | status flip + transition |
| apply-item | human (always, since suggested links/supersedes may touch LT) | executes the suggested link/supersede + queue resolve + transitions (`payload.item_id`); refuses `conflict` items (`InvalidTransition` pointing to resolve-conflict) |
| resolve-conflict | human (always) | `conflict` item only; exactly one of: winner_id (same-tier: supersede winner over loser, full existing supersede rules; LT winner/ST loser: discard the loser instead; ST winner/LT loser: refused, `InvalidResolution` → `promote_refined`) / refined (same-tier: new decision supersedes both sides, long-term when that shared tier is; mixed-tier: refused, `InvalidResolution` → `promote_refined`) / neither+reason (CONFLICTS_WITH link + `conflict_accepted` transitions on both, no status change) + queue resolve |
| dismiss-item | human | queue resolve as dismissed + transition; record untouched (works on `conflict` items too — a false positive) |

## Enforcement mechanics

- **Authority (I-2)**: every act declares its required actor kinds; the engine
  checks the attested `Actor.kind` before opening the transaction and raises
  `AuthorityViolation` — never a silent no-op.
- **Serialization (I-1)**: every act's transaction opens `SELECT … FOR UPDATE`
  on all touched decisions; queue resolutions use the guarded conditional
  UPDATE. Status writes always carry `new_status` in the transition (the
  computable fold).
- **Legality**: each act validates the current status against the machine
  (`InvalidTransition` names both states). The status it writes goes through the
  store's paired `apply_transition` only.
- **Acyclicity**: supersede walks the successor chain (recursive CTE) before
  linking; a cycle raises `InvalidTransition`.
- **Refined promotion**: validates the refined `NewDecision` like any recording
  (registry, refs), forces `tier=long_term`, `recorded_by=` the promoting human;
  sources must all be short_term `current` (or `not_promoted` — re-recommended
  path, FR-4.5).
- **Pending-supersede execution at the gate**: promote scans the promoted
  decision's queued LT-supersede claims and executes them inside the same
  transaction — the only door such claims pass through.
- **Conflict resolution** (FR-5.4): a `conflict` item names two decisions, both
  `current` when discovery filed it. `winner_id`, when both sides share a
  tier, reuses `supersede`'s own validation and execution helpers verbatim —
  the tier gate, acyclicity check, and loser auto-void all apply unchanged.
  Mixed tiers are NOT routed through supersede at all (FR-5.2a: no direct
  LT-over-ST or ST-over-LT SUPERSEDES link exists): a long-term winner over a
  short-term loser discards the loser (`_ST_DISCARDABLE` status check,
  `discarded` transition with `reason` falling back to the item's own
  rationale, then the usual open-item auto-void); a short-term winner over a
  long-term loser raises `InvalidResolution` naming `promote_refined`.
  `refined` validates a NEW `NewDecision` like any recording — legal only
  when both sides share a tier (a mixed-tier pair raises the same
  `InvalidResolution` redirect, upfront, before any insert) — forces its
  tier to that shared tier (`long_term` mirrors `record_long_term`'s
  `recorded`+`promoted` pair), then supersedes both sides; no acyclicity
  check is needed for either side (the refined decision is a freshly minted
  id with no existing links, same reasoning as the pending-supersede case
  above). Accepting a conflict (neither `winner_id` nor `refined`, reason
  required) inserts a `CONFLICTS_WITH` link and a `conflict_accepted`
  transition on both sides with `new_status=None` — no status change,
  mirroring `supplement`'s
  shape (FR-5.3).

## Acceptance

- Exhaustive transition-matrix test: every (act × actor-kind × current-status)
  cell asserts allowed/`AuthorityViolation`/`InvalidTransition` per the tables
  above — the matrix in the test file IS the readable spec of record.
- Property test: any generated legal act sequence leaves every decision's status
  equal to the fold of its transitions, and leaves content columns byte-identical
  to insertion.
- Refined-promotion test: multi-source consolidation produces one LT row, N
  `PROMOTED_FROM` links, N `promoted` sources, `refined: true` payloads.
- Cycle test: A supersedes B, B supersedes A refused.
- Conflict-resolution tests: all three same-tier `resolve_conflict` paths
  happy (winner supersedes loser; refined consolidates both sides,
  short-term and long-term; accept links + transitions both sides with no
  status change and surfaces in `history()`); authority, wrong-item-kind, and
  illegal-argument-combination refusals; the mixed-tier ruling (LT winner
  discards its ST loser, falling back to the item's rationale when no reason
  is given; ST winner over an LT loser and mixed-tier `refined` both redirect
  to `promote_refined` via `InvalidResolution`); `apply_item` refusing a
  `conflict` item; `dismiss_item` still resolving one as a false positive; a
  second accept on a re-discovered pair no-ops the link while still logging
  its own `conflict_accepted` pair; two concurrent resolutions of the same
  item (exactly one wins, the other `ItemAlreadyResolved`) and a winner-path
  resolution racing a direct `supersede()` of the loser (exactly one wins,
  the other `InvalidTransition`, fold intact) — mirroring
  `test_promote_vs_supersede_race`. The property random walk also seeds and
  resolves `conflict` items (winner either direction, or accept) between
  same-tier `current` decisions under randomized interleaving. A
  `recorded_at` tie between two discovery candidates is tie-broken by
  `decision_id` (see 04) so a symmetric kind like `conflicts` never produces
  a reversed-pair duplicate.
