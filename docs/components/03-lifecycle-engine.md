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

**short_term**: `current → promoted | not_promoted | superseded | discarded |
archived`; `not_promoted → current-for-consideration` via re-recommendation
(status stays `not_promoted` until a later promote/decline resolves it — the
queue item, not the status, carries pendingness); `archived → (prior status)` on
reactivation. Terminal: `promoted`, `superseded`, `discarded`.

**long_term**: `current → superseded`. Terminal: `superseded`. Never archived.

Pendingness (recommendations, suggested links/supersedes) lives ONLY in `queue`
rows — a pending item never changes any status (I-4).

## Acts (each = one store transaction, I-1)

| Act | Who may | Effect |
|---|---|---|
| record | any actor | decision row + `recorded` transition (+ immediate ST-target supersede links; LT-target claims → queue) |
| record_long_term | human | LT row + `recorded` + `promoted` transitions (FR-4.4) |
| recommend | any actor | queue item + `recommended` transition |
| promote (verbatim) | human | LT copy + `PROMOTED_FROM` link + pending-supersede execution + source→`promoted` + queue resolve + transitions |
| promote_refined | human | as promote, but LT row = human-authored content, ≥1 sources each linked+`promoted`; transitions carry `refined: true` (FR-4.6) |
| decline | human | source→`not_promoted` + queue resolve + transition with reason |
| discard | recorder-of-own-ST or human | →`discarded` + transition (FR-3.3) |
| supersede | actor; human if target is long_term | link + target→`superseded` + transitions on both |
| supplement | actor; human if target is long_term | link + `supplement_linked` transitions; NO status change (FR-5.3) |
| archive / reactivate | engine (sweep) / any-on-own, human-on-any | status flip + transition |
| dismiss-link-item | human | queue resolve + transition; record untouched |

## Enforcement mechanics

- **Authority (I-2)**: every act declares its required actor kinds; the engine
  checks the attested `Actor.kind` before opening the transaction and raises
  `AuthorityViolation` — never a silent no-op.
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
