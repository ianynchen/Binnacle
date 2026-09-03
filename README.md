# Binnacle

A PostgreSQL-backed decision-record library: the fleet's decision record and
precedent engine. Binnacle stores decisions (scenario / outcome / reasoning),
tracks their lifecycle (record → recommend → promote → supersede/supplement →
archive) under a human-gated write path, and answers precedent queries over
pgvector embeddings. It is a library, not a service — no daemon, no env/file
reads, no authorization logic (see "Actor attestation" below). See
[`docs/REQUIREMENTS.md`](docs/REQUIREMENTS.md) and
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full contract.

## Contents

[What binnacle is](#what-binnacle-is) · [Install](#install) ·
[Provisioning](#provisioning-preconditions) ·
[Configuration](#configuration) · [Quickstart](#quickstart) ·
[Core concepts](#core-concepts) ·
[Recording decisions](#recording-decisions) ·
[Promotion workflow](#the-promotion-workflow) ·
[Relationships](#relationships) · [Conflicts](#conflicts) ·
[Query scenarios](#query-scenarios) · [Sweeps and ports](#sweeps-and-ports) ·
[Export](#export) · [Error handling](#error-handling) ·
[Development](#development) · [Running the tests](#running-the-tests) ·
[Actor attestation](#actor-attestation) ·
[Embedding text convention](#embedding-text-convention) ·
[Limitations](#limitations--known-v1-gaps)

## What binnacle is

Binnacle is a decision record for a fleet of humans and AI agents working on
the same codebase: a place to write down *what was decided, why, and by whom*
so both future humans and future agent sessions can find it again instead of
re-litigating it.

Every decision lives in one of two tiers:

- **short_term** — the working record. Anything may land here: an agent
  mid-task settling a design question, a human's note, a duplicate that
  turns out to be noise. Cheap to write, cheap to supersede, auto-archived
  once nobody touches it for a while.
- **long_term** — the durable record: standing policy. Entered **only**
  through the human gate — an agent can *propose* a long-term decision but
  can never make one durable itself. This is the authority rule the whole
  design turns on: mechanism decides nothing about *which* decisions matter,
  but rigidly enforces *who* may make one permanent.

Nothing is ever edited or deleted. A correction is a new decision that
supersedes the old one; junk is `discarded` (hidden from default reads,
never removed). Every status change is an **append-only transition**
`(action, actor, timestamp, reason?, payload?)` — the transition log *is*
the audit trail, and a decision's status is nothing more than the fold of
its own transitions. "Prove agents never promoted anything" is answerable by
querying that log, not by trusting anyone's word for it.

Binnacle's core is deliberately **LLM-free** (FR-7.1): it never constructs or
calls a model itself. Where judgment helps — classifying whether two
decisions conflict, embedding text for similarity search — it defines a
narrow port (`Embedder`, `Suggester`) and takes an implementation from the
caller. Without a `Suggester`, recording, promotion, relationships, and every
query still work fully; binnacle just never proposes anything on its own.

## Install

```bash
uv add "binnacle @ git+https://github.com/ianynchen/Binnacle.git"
# or
pip install "binnacle @ git+https://github.com/ianynchen/Binnacle.git"
```

Requires Python ≥3.13 and PostgreSQL 18 with the `pgvector` extension
available.

## Provisioning preconditions

Binnacle ships schema migrations (`Binnacle.migrate()`) but does **not**
provision the database, role, or extension it runs in — that is the host's
job, performed once by a privileged role before the library is ever used:

```sql
-- as a privileged role (e.g. postgres superuser)
CREATE DATABASE my_app;
\c my_app
CREATE EXTENSION vector;
```

Binnacle then owns and migrates only its own schema inside that database
(default name `binnacle`, configurable via `BinnacleConfig.schema_name`) —
it does not touch tables outside that schema, and does not need superuser
rights itself, only `CREATE`/`USAGE` on its schema. `migrate()` checks for
the `vector` extension and raises `ConfigError` (never attempts to create it
itself) if it is missing.

### Multiple instances, or multiple schemas in one database

`BinnacleConfig` carries no global or process-wide state (FR-8.1): every
`Binnacle(config)` is independent, so one process can hold more than one
client against the same database by giving each a distinct `schema_name` —
a staging instance alongside production, or per-tenant isolation in one
Postgres instance:

```python
dsn = "postgresql://localhost:5432/app"
prod = Binnacle(BinnacleConfig(dsn=dsn, embedder=DemoEmbedder()))
staging = Binnacle(BinnacleConfig(dsn=dsn, schema_name="binnacle_staging", embedder=DemoEmbedder()))
await prod.migrate()
await staging.migrate()
# decisions, domains, and queue are fully isolated per instance --
# they may share a database, never a schema.
```

(`DemoEmbedder` is the same illustrative stand-in used in the
[Quickstart](#quickstart) below.)

Each instance may instead share one caller-supplied connection pool
(`BinnacleConfig.pool=`, in place of `dsn=`) when the host already manages
pooling — `dsn` and `pool` are mutually exclusive, and `Binnacle.aclose()` is
then a no-op, since the caller owns closing the pool it supplied.

## Configuration

Every field on `BinnacleConfig`, validated at construction (no I/O —
`Binnacle(config)` itself performs none either; `migrate()` is the explicit,
host-invoked I/O step):

| Field | Default | Meaning |
|---|---|---|
| `dsn` | `None` | A `postgresql://` connection string. Exactly one of `dsn`/`pool` is required. |
| `pool` | `None` | A caller-supplied `psycopg` async pool, in place of `dsn` — the host owns opening/closing it. |
| `schema_name` | `"binnacle"` | The Postgres schema this instance owns exclusively (§4.1). Must match `^[a-z_][a-z0-9_]{0,62}$`. |
| `embedder` | *(required)* | An `Embedder` port fulfillment — see [Sweeps and ports](#sweeps-and-ports). |
| `suggester` | `None` | A `Suggester` port fulfillment. `None` disables discovery's classification half entirely. |
| `embedding_dim` | `768` | Must match both the migrated `VECTOR(n)` column and every vector your `Embedder` returns (`EmbeddingDimensionMismatch` otherwise). `768` is nomic-embed-text-v1.5's width. |
| `archival_age_days` | `90` | FR-3.4: a short-term decision untouched this long auto-archives. Discovery's "aging unrecommended" window is half of this value. |
| `compact_outcome_chars` | `200` | FR-6.7: characters of `outcome` the compact projection truncates to, in SQL. |
| `discovery` | `DiscoveryConfig()` | FR-7.4 discovery-sweep tuning, below. |

`DiscoveryConfig` (nested under `discovery=`), each knob validated at
construction (`ConfigError` on an out-of-range value):

| Field | Default | Meaning |
|---|---|---|
| `k` | `10` | Nearest neighbors considered per newly embedded decision (`1..10`, FR-7.4's cap). |
| `confidence_floor` | `0.6` | A `Suggester` classification below this confidence (`0.0..1.0`) is dropped, not enqueued. |
| `per_sweep_cap` | `50` | Max relationship-suggestion queue items one `discover()` call may enqueue (`>=1`); excess candidates wait for the next sweep. |

```python
from binnacle import BinnacleConfig, DiscoveryConfig

config = BinnacleConfig(
    dsn="postgresql://localhost:5432/binnacle_test",
    embedder=my_embedder,
    suggester=my_suggester,  # omit (or None) to disable discovery classification
    discovery=DiscoveryConfig(k=8, confidence_floor=0.7, per_sweep_cap=25),
)
```

## Quickstart

```python
import asyncio
from binnacle import Actor, Binnacle, BinnacleConfig, NewDecision


class DemoEmbedder:  # stand-in for a real Embedder (e.g. nomic-embed-text-v1.5)
    async def embed(self, texts: list[str]) -> list[list[float]]:  # zero vectors, illustrative only
        return [[0.0] * 768 for _ in texts]


async def main() -> None:
    config = BinnacleConfig(
        dsn="postgresql://localhost:5432/binnacle_test", embedder=DemoEmbedder()
    )
    bn = Binnacle(config)
    await bn.migrate()

    human, agent = Actor("human", "alice"), Actor("agent", "meridian/sess-1")
    await bn.add_domain("architecture", "system design decisions", actor=human)

    nd = NewDecision(
        domain="architecture",
        scenario="how to handle transient ingestion failures?",
        outcome="retry with exponential backoff, capped at 3 attempts",
        reasoning="avoids thundering herd on recovery",
        source="meridian",
    )
    decision = await bn.record(nd, actor=agent)
    await bn.recommend(decision.decision_id, actor=agent, reason="stable after a week")
    await bn.promote_refined([decision.decision_id], refined=nd, actor=human)

    for hit in await bn.precedent("how do we handle flaky network calls?"):
        print(hit.decision.outcome_truncated, hit.similarity)


asyncio.run(main())
```

`DemoEmbedder` above is illustrative only (it returns zero vectors, so
`precedent()` similarity scores are meaningless) — production callers supply
a real `Embedder` (meridian fulfills it via `nomic-embed-text-v1.5`; tests
use the deterministic `StubEmbedder` in `tests/helpers.py`, not shipped in
the package). Every snippet from here on assumes the same async context
(`bn`, `human`, `agent` as constructed above).

## Core concepts

**Tiers and statuses.** `short_term` has the fuller exit matrix; `long_term`
is deliberately narrow:

| short_term status | Legal exits |
|---|---|
| `current` | `promoted` (gate), `not_promoted` (gate), `superseded` (any actor), `discarded` (recorder-of-own or human), `archived` (clock; blocked by open queue items) |
| `not_promoted` | `promoted` (gate, after re-recommendation), `superseded`, `discarded` (human), `archived` (clock) |
| `archived` | reactivated → restored prior status; `discarded` (human) |
| `promoted` / `superseded` / `discarded` | terminal |

| long_term status | Legal exits |
|---|---|
| `current` | `superseded` (human, only by another long_term decision) |
| `superseded` | terminal |

`not_promoted` means "considered at the gate and declined, kept as signal";
`discarded` means "not a real decision" (noise, malformed, duplicate) —
distinct statuses (FR-3.3). `archived` and `not_promoted` are always
revivable; `promoted`, `superseded`, and `discarded` are not.

**Actors.** Every write-path verb takes an explicit `Actor(kind, id)`, where
`kind ∈ {"human", "agent", "engine"}` — attestation, not authentication (see
[Actor attestation](#actor-attestation)), but it is how every authority
rule is enforced.

**Refs.** `Ref(role, kind, identifier, note=None)` attaches external context
to a decision: `role` is `"subject"` (what it *applies to* — a component,
product, document node; absence means "applies generally") or `"evidence"`
(what *supports* it — a session id, a spike doc, a URL). `kind`/`identifier`
are open strings. Subject scoping drives `relevant()`'s matching: a query
for subject X returns decisions whose subject refs include X **or**
decisions with no subject refs at all.

**The queue.** Every recommendation, discovered relationship, and detected
conflict is a row in one queue, never a status change on its own (I-4:
pendingness lives only in `queue` rows). A human resolves each item —
execute it, decline/dismiss with a reason, or leave it open (which blocks
that decision's auto-archival). `QueueKind` is `promote` / `link` /
`supersede` / `conflict`.

**Transitions as audit.** `TransitionAction` is a closed set — `recorded`,
`recommended`, `promoted`, `declined`, `discarded`, `superseded`,
`supplement_linked`, `archived`, `reactivated`, `voided`, `dismissed`,
`conflict_accepted` — and every one is who/when/why, permanently. There is
no separate audit-log feature to fall out of sync; `bn.history()` and
`bn.changes()` both read straight from this log.

## Recording decisions

`bn.record()` accepts any actor and always lands in `short_term`:

```python
from binnacle import NewDecision, OptionConsidered, Ref

nd = NewDecision(
    domain="architecture",
    scenario="how should transient ingestion failures be handled?",
    outcome="retries use exponential backoff, capped at 3 attempts",
    reasoning="bounded retries avoid unbounded resource consumption on persistent failures",
    source="meridian",
    options_considered=[
        OptionConsidered(option="fixed-interval retry", why_rejected="thundering herd on recovery")
    ],
    refs=[
        Ref(role="subject", kind="component", identifier="portolan-ingest", note=None),
        Ref(role="evidence", kind="session", identifier="sess-1", note=None),
    ],
    confidence=0.8,  # 0..1, a triage signal -- primarily meaningful for agent sources
)
decision = await bn.record(nd, actor=agent)
```

Recording never awaits the `Embedder` (I-5) — the decision is immediately
queryable by domain/subject/status; its embedding is computed minutes later
by the [backfill sweep](#sweeps-and-ports). Recording into an unregistered
domain raises `UnknownDomain`; into a deactivated one, `InactiveDomain`.

**Idempotent recording** (FR-1.6): pass your own `decision_id` and retry
freely — an identical retry (hash-compared over content, excluding
`metadata`) is a silent no-op returning the existing decision; a *different*
payload under the same id raises `IdempotencyConflict`. **Declaring
relationships at write time**: `supersedes=`/`supplements=` link to
existing decisions in the same atomic write (FR-1.4) — short-term ↔
short-term supersession is ungated, so an agent mid-session can supersede
its own earlier call in one step (a claim against a **long-term** decision
instead waits as a pending claim, executed at promotion time, FR-5.2):

```python
from uuid import uuid4

my_id = uuid4()
first = await bn.record(nd.model_copy(update={"decision_id": my_id}), actor=agent)
retry = await bn.record(nd.model_copy(update={"decision_id": my_id}), actor=agent)
assert first.decision_id == retry.decision_id  # same content, same id -> no-op

batching = await bn.record(
    nd.model_copy(
        update={
            "outcome": "batch writes so retries are unnecessary",
            "supersedes": [decision.decision_id],
        }
    ),
    actor=agent,
)
```

**Direct long-term recording** (FR-4.4, human only) skips the queue for a
deliberate durable decision a human makes directly — "record + promote" as
one atomic act, raising `AuthorityViolation` for any non-human actor:

```python
dead_lettering = await bn.record_long_term(
    NewDecision(
        domain="architecture",
        scenario="how should queue-fed ingestion handle repeated failures?",
        outcome="queue-fed ingestion additionally uses dead-lettering",
        reasoning="a dead-letter queue captures what backoff alone can't resolve",
        source="meridian",
    ),
    actor=human,
)
```

## The promotion workflow

Promotion is the only door into the long-term tier, and always runs through
one of three human-gated verbs.

**`recommend()`** files a pending-promote queue item — any actor may call it
(an agent at session end, the discovery sweep nominating aging decisions, or
a human self-recommending). **`promote()`** then executes that item
**verbatim** (human only): in one transaction, a long-term copy is created
(`PROMOTED_FROM` link back to the source), any pending long-term claim the
source made executes now, the source flips to `promoted`, and the queue item
resolves:

```python
item_id = await bn.recommend(
    batching.decision_id, actor=agent, reason="this is standing policy, not session detail"
)
promoted = await bn.promote(item_id, actor=human)
```

**`promote_refined()`** (FR-4.6) is the same gate, but the human authors the
long-term content instead of copying it verbatim — from **one or more**
short-term sources. This is how "one service's retry decision becomes
policy for all remote calls, with jitter added to the backoff" happens:
every source is marked `promoted` with its own `PROMOTED_FROM` link, and the
promotion transitions carry `refined: true` so the source/refined diff
stays permanently visible. Passing more than one source id consolidates
several related short-term decisions into one long-term policy:

```python
refined = await bn.promote_refined(
    [batching.decision_id],
    refined=NewDecision(
        domain="architecture",
        scenario="standardize retry backoff across all remote calls",
        outcome="all remote calls use exponential backoff with jitter, capped at 3 attempts",
        reasoning=(
            "jitter avoids synchronized retry storms across services; "
            "the policy generalizes beyond ingestion"
        ),
        source="meridian",
    ),
    actor=human,
)
```

**Declining** (human only) is not a rejection of the content, just of
promoting it *now* — the source becomes `not_promoted`, never terminal
(FR-4.5: re-recommendable later):

```python
await bn.decline(item_id, actor=human, reason="style bikeshedding, not a real policy")
```

**What auto-voids.** When a decision leaves `current` outside the gate —
superseded or discarded — its own open queue items resolve as `voided` in
the same transaction. Promoting or refining likewise resolves the queue
items belonging to every source it consumes.

## Relationships

Relationships are an append-only graph over decisions: `SUPERSEDES`,
`SUPPLEMENTS`, `CONFLICTS_WITH`, and the internal `PROMOTED_FROM`
provenance link created by promotion. They arise from declaration at write
time (above), confirmation of a discovered suggestion (`apply_item()`),
post-hoc human curation, or `resolve_conflict()`'s accept path.

**Supersede** (`bn.supersede(new_id, old_id, actor)`) replaces `old_id`: the
old decision stays readable, flips to `superseded`, and links to its
successor. **Supplement** (`bn.supplement(new_id, old_id, actor)`) qualifies
or extends a decision that remains `current` — no status change at all
(FR-5.3); binnacle records the relationship, never adjudicates which
decision "wins" when a reader sees both.

**Tier symmetry (FR-5.2a).** A long-term decision may only be superseded by
another long-term decision — directly by a human, or by the promoted copy
when a pending short-term claim executes at the gate. A short-term decision
can never directly supersede a long-term one; its only path in is
promotion. Superseding or supplementing a long-term decision always
requires a human actor:

```python
await bn.supplement(dead_lettering.decision_id, refined.decision_id, actor=human)

mesh_retry = await bn.record_long_term(
    NewDecision(
        domain="architecture",
        scenario="how should remote-call failures be handled after the service-mesh migration?",
        outcome="remote calls rely on the service mesh's retry and circuit breaking",
        reasoning="the mesh sidecar now owns retry policy uniformly",
        source="meridian",
    ),
    actor=human,
)
await bn.supersede(mesh_retry.decision_id, refined.decision_id, actor=human)
```

**Acyclicity.** Before linking a new `SUPERSEDES` edge, the lifecycle engine
walks the successor chain; closing a cycle (A supersedes B, B supersedes A)
raises `InvalidTransition` and nothing is written.

## Conflicts

`discover()`'s `conflicts` classification (see
[Sweeps and ports](#sweeps-and-ports)) files a `conflict` queue item naming
two `current` decisions whose outcomes are in tension — never adjudicated
automatically, always left for `resolve_conflict()` (human only). Exactly
one of three arguments resolves the item:

```python
conflict = (await bn.queue(kinds=["conflict"]))[0]

# a declared winner, when both sides share a tier -- reuses supersede()'s
# own rules (tier gate, acyclicity, auto-void of the loser's open items)
await bn.resolve_conflict(conflict.item.item_id, actor=human, winner_id=some_decision_id)

# a refined, consolidating decision, legal only when both sides share a
# tier -- a new human-authored decision supersedes BOTH sides at once
await bn.resolve_conflict(
    conflict.item.item_id,
    actor=human,
    refined=NewDecision(
        domain="architecture",
        scenario="timeout for the search API",
        outcome="timeout is 1000ms, split the difference",
        reasoning="balances fast-fail against false negatives under load",
        source="meridian",
    ),
)

# acceptance, with neither argument -- a standing CONFLICTS_WITH link on
# both sides, no status change (FR-5.3's "reader resolves meaning" stance)
await bn.resolve_conflict(
    conflict.item.item_id, actor=human, reason="both are valid for different traffic tiers"
)
```

**Mixed tiers are handled explicitly, not refused uniformly.** There is no
cross-tier `SUPERSEDES` link (FR-5.2a): a **long-term winner over a
short-term loser** DISCARDS the loser instead of superseding it (human
authority already suffices to discard any short-term decision, FR-3.3); a
**short-term winner over a long-term loser** — and a mixed-tier `refined` —
has no such mechanism, since there is no way for a short-term decision to
out-rank durable policy directly. Both of the latter raise
`InvalidResolution` pointing at `promote_refined`, the one door a
short-term decision has into long-term change:

```python
from binnacle.domain.errors import InvalidResolution

# each resolve_conflict call fully resolves its item -- these are two
# separate conflicts, not two attempts at the same one
await bn.resolve_conflict(
    lt_over_st_conflict.item.item_id, actor=human, winner_id=long_term_policy_id
)
# the short-term loser's status is now "discarded", not "superseded"

try:
    await bn.resolve_conflict(
        st_over_lt_conflict.item.item_id, actor=human, winner_id=short_term_id
    )
except InvalidResolution:
    ...  # consolidate through promote_refined instead
```

`apply_item()` (executing a discovered `supersede`/`link` suggestion) never
accepts a `conflict` item — it raises `InvalidTransition` pointing at
`resolve_conflict`. `dismiss_item()`, by contrast, works on any item kind
including `conflict` — a human can always dismiss a false positive without
adjudicating anything.

## Query scenarios

Every read below runs through the public `Binnacle` client; none requires a
`Suggester`, and only `precedent()` requires an `Embedder` with a real model
behind it to be *meaningful* (the call itself always works).

```python
from datetime import UTC, datetime, timedelta

# an agent starting a session -- compact, top-N relevance for what it's
# about to touch, one-liners not reasoning blobs (context is a budget)
context = await bn.relevant(
    domains=["architecture", "testing"], subject=("component", "portolan-ingest"), limit=20
)

# why does this decision exist -- content, transitions in order, both
# supersession chains
h = await bn.history(decision.decision_id)
print(h.decision.reasoning, [t.action for t in h.transitions])
print([p.outcome for p in h.predecessors], [s.outcome for s in h.successors])

# have we decided something like this before -- precedent deliberately
# includes dead history (superseded, not_promoted), labeled via status
hits = await bn.precedent("how should retry backoff be configured?", domains=["architecture"])
for hit in hits:
    print(hit.decision.status, hit.similarity, hit.decision.outcome_truncated)

# what needs my review -- oldest-first, or by how shaky the confidence is
# (item confidence, falling back to the decision's own, then 1.0)
by_age = await bn.queue(kinds=["promote", "conflict"], order="oldest")
shakiest = await bn.queue(order="shakiest")

# what changed while I was away -- transitions since a checkpoint, paired
# with each decision's compact projection
since_monday = datetime.now(UTC) - timedelta(days=4)
for transition, compact in await bn.changes(since=since_monday):
    print(transition.action, transition.actor, compact.outcome_truncated)

# audit: prove agents never promoted anything -- the same feed, filtered,
# is the proof, not an assertion
agent_promotions = await bn.changes(actions=["promoted"], actor=agent)
assert agent_promotions == []  # promote()/promote_refined() are human-only

# batch access -- following an id from another system, or a source system
# listing its own decisions
resolved = await bn.get_many([some_id, another_id])
portolan_decisions = await bn.by_source("portolan", status=["current"])

# time travel -- as_of honors valid_from/until as of a past moment, even
# after a decision has since expired
before_expiry = await bn.relevant(domains=["architecture"], as_of=some_past_timestamp)

# including archived decisions -- off by default, always retrievable
with_archived = await bn.relevant(
    domains=["architecture"], status=["current", "archived"], include_archived=True
)
```

**Compact vs. full projections.** `relevant()`'s `projection=` is
`Literal["compact", "full"]`; passing a literal (the default, `"compact"`)
narrows the static return type to `list[CompactDecision]` at the call site,
and `projection="full"` narrows to `list[Decision]` — `mypy --strict`
enforces this via `@overload`, so `d.outcome_truncated` type-checks on a
compact result and `d.reasoning` on a full one, with no cast needed:

```python
compact = await bn.relevant(domains=["architecture"])  # list[CompactDecision]
full = await bn.relevant(domains=["architecture"], projection="full")  # list[Decision]
```

## Sweeps and ports

Binnacle defines its I/O boundary to judgment as two ports (FR-7.1) — it
never constructs an LLM client or embedding model itself. Recording,
promotion, and relationships all work without either port wired up; only
precedent search and discovery need them.

**`Embedder`** — `async def embed(self, texts: list[str]) -> list[list[float]]`,
preserving order and length. See
[Embedding text convention](#embedding-text-convention) for the exact text
convention a custom `Embedder` must match.

**`Suggester`** — two methods, both host-scheduled inputs to `discover()`:

```python
class Suggester(Protocol):
    async def classify_pairs(self, pairs: list[CandidatePair]) -> list[Suggestion]: ...
    async def assess_promotion(
        self, decisions: list[CompactDecision]
    ) -> list[PromotionAssessment]: ...
```

`classify_pairs` receives one `CandidatePair` (`decision`, `other`,
`similarity`) per structurally-related k-NN neighbor of a newly embedded
decision and returns one `Suggestion(kind, rationale, confidence)` per pair
— `kind` is `supersedes` / `supplements` / `conflicts` / `unrelated`.
`assess_promotion` receives short-term `current` decisions aged without a
recommendation and returns one `PromotionAssessment(decision_id, recommend,
rationale, confidence)` per decision — a positive `recommend` files a real
recommendation (`proposed_by engine:binnacle`), audited the same as a human
or agent recommendation.

**Hosting the three sweeps.** All three are plain host-invoked library
calls — bounded, idempotent, safe to schedule however the host likes (cron,
a background task, a manual button):

```python
await bn.backfill_embeddings(batch=100)  # unembedded backlog -> Embedder -> stored vectors
await bn.discover(batch=100)  # newly embedded decisions -> Suggester -> queue items
await bn.archive_stale()  # FR-3.4 clock rule -> archived transitions
```

`discover()` is cursor-driven (`embeddings.discovered_at IS NULL`), so a
sweep that dies mid-run resumes exactly where it left off; it is also O(k)
per newly embedded decision by construction (FR-7.4) — never an all-pairs
scan, regardless of how large the record grows.

**No `Suggester` configured** (`suggester=None`, the default) makes
`discover()` a clean no-op — every `DiscoverySummary` counter comes back
zero, and neither cursor is even read. Backfill and archival are
unaffected; only relationship discovery and engine-nominated promotions
need a `Suggester`.

## Export

`bn.export(domains=None, tier=None, status=None)` returns a JSON-safe
`dict` — every `UUID` as `str`, every `datetime` as ISO-8601 UTC, every
`Actor` as `"kind:id"` — ready for `json.dumps` with no further conversion:

```python
bundle = await bn.export(domains=["architecture"])
# {
#   "schema_version": 1,
#   "decisions": [...],  # each carrying its own refs, inline
#   "links": [...],
#   "transitions": [...],
#   "domains": [...],    # the full domains registry, unfiltered
# }
```

Filtered decisions come with every link and transition touching them, plus
the complete domains registry regardless of the filter. Embeddings are
deliberately excluded — derived and rebuildable via `backfill_embeddings()`,
not part of the durable record. `schema_version` is the export document's
own shape version (separate from each decision's own `schema_version`
field). There is currently no corresponding `import` — see
[Limitations](#limitations--known-v1-gaps).

## Error handling

Every error binnacle raises is a typed subclass of `BinnacleError`, so
callers can branch on kind rather than parsing messages:

```python
from binnacle import BinnacleError, IdempotencyConflict, InactiveDomain, UnknownDomain

try:
    await bn.record(nd, actor=agent)
except UnknownDomain:
    ...  # nd.domain isn't in the registry -- register it first
except InactiveDomain:
    ...  # nd.domain was deactivated -- reactivate via add_domain, or pick another
except IdempotencyConflict:
    ...  # nd.decision_id already exists with DIFFERENT content -- a real conflict, not a retry
except BinnacleError:
    ...  # catch-all for anything else in the hierarchy
```

| Error | Raised when |
|---|---|
| `ConfigError` | Bad `BinnacleConfig` construction — `dsn`+`pool` both set, an invalid `schema_name`, an out-of-range discovery knob. |
| `UnknownDomain` / `InactiveDomain` | Writing into an unregistered / deactivated domain (FR-2.1/2.2). |
| `DecisionNotFound` | `history()` (or similar) on an id that doesn't exist. |
| `InvalidTransition` | An illegal status change — names both the current status and the attempted action. |
| `AuthorityViolation` | Wrong actor kind for the verb — e.g. an agent calling `promote()`. |
| `IdempotencyConflict` | A caller-supplied `decision_id` already exists with different content (FR-1.6). |
| `EmbeddingDimensionMismatch` | An `Embedder`/`migrate()` vector-width disagreement. |
| `ItemNotFound` / `ItemAlreadyResolved` | A queue-item id that doesn't exist, or was already resolved by a concurrent call. |
| `InvalidResolution` | `resolve_conflict()`'s argument combination is malformed, or names an unsupported mixed-tier path — see [Conflicts](#conflicts). |

`InvalidResolution` is defined alongside the rest of the hierarchy in
`binnacle.domain.errors` but is not re-exported from the top-level
`binnacle` package today — import it from `binnacle.domain.errors` directly,
as in [Conflicts](#conflicts), or catch `BinnacleError` if you don't need to
distinguish it.

## Development

### The guardrail stack

- **`pre-commit`** (`.pre-commit-config.yaml`): `gitleaks` (hardcoded-secret
  scanning) and `ruff` (lint + format), run on every commit —
  `pre-commit install` once, then `pre-commit run --all-files` to check
  everything.
- **CI** (`.github/workflows/ci.yml`, GitHub Actions): on every push/PR to
  `main`, spins up a `pgvector/pgvector:pg18` Postgres service, runs
  `scripts/check.sh` (ruff format/lint, mypy strict, import-linter,
  `pytest`) and `pre-commit run --all-files`.
- **import-linter** (`pyproject.toml` `[tool.importlinter]`): enforces the
  layering `binnacle.adapters → binnacle.application → binnacle.domain`,
  and that `domain`/`application` stay free of DB-driver imports
  (`psycopg`, `yoyo`, `pgvector`).

Run everything locally with `bash scripts/check.sh`.

## Running the tests

Integration tests need a live Postgres with `pgvector` installed. They read
`BINNACLE_TEST_DSN` (default `postgresql://localhost:5432/binnacle_test`)
and **skip cleanly** (`pytest.skip`) when that DSN is unreachable, so
`pytest`/`scripts/check.sh` runs anywhere — unit-only when no database is
reachable, the full suite when one is.

```bash
createdb binnacle_test
psql binnacle_test -c "CREATE EXTENSION vector"
export BINNACLE_TEST_DSN=postgresql://localhost:5432/binnacle_test
uv run pytest
```

## Actor attestation

Every write-path verb takes an explicit `Actor(kind, id)` — binnacle **never
guesses or infers** an actor's kind. The caller (meridian) authenticates its
users/agents and attests `kind ∈ {human, agent, engine}` at the call site;
binnacle enforces the authority rule (e.g. "promotion requires a human") only
against the attested `kind`, and records `id` as given. Id honesty *within*
a kind (e.g. one agent claiming another agent's id) is the caller's
enforcement duty, not binnacle's — this is attribution, not authorization
(FR-8.2, ARCHITECTURE I-2/DR-5).

## Embedding text convention

The text embedded for a decision (both at backfill and at discovery
re-embedding time) is always:

```python
"\n\n".join([decision.scenario, decision.outcome, decision.reasoning])
```

fulfilled by `nomic-embed-text-v1.5` (768 dimensions, 8192-token context —
OQ-3), matching `BinnacleConfig.embedding_dim`'s default of `768`. A caller
supplying a custom `Embedder` must embed the same convention for `precedent()`
similarity scores to mean anything, and must set `embedding_dim` to match its
own vector width.

## Limitations / known v1 gaps

Binnacle v1 deliberately leaves the following out of scope; see
[`docs/REQUIREMENTS.md` §5](docs/REQUIREMENTS.md) for the full v2 list and
named adoption triggers:

- **No import path.** `export()` produces a JSON bundle; there is no
  corresponding `import` to load one back into a store (REQUIREMENTS §5).
- **Precedent over-fetch escalates, but only up to a hard cap.**
  `precedent()` over-fetches candidates at a `4×` factor when
  `domains`/`tiers`/`include_dead=False` will drop some (matching
  `store.knn`'s own over-fetch factor), and re-queries at `k × 4` — excluding
  ids already seen — when a round still under-fills the result and the index
  looks like it has more to give. Escalation stops at the first of 3 rounds
  or `k` reaching 1024, whichever comes first: a filter narrow enough to
  exhaust that cap without filling `limit` still returns fewer than `limit`
  results (`src/binnacle/application/query.py`).
- **Discovery re-embeds rather than reading back stored vectors.** The
  `StorePort` has no "read one embedding back" primitive, so `discover()`
  re-derives a subject decision's vector by re-embedding its text for its
  own k-NN lookup, rather than reading the vector already stored by
  `backfill_embeddings()` (`src/binnacle/application/discovery.py`).
- **Reversed-pair suggestions on equal `recorded_at`.** Discovery's temporal
  filter allows `other.recorded_at <= subject.recorded_at`; when two
  decisions share the exact same `recorded_at`, each can appear as the
  other's "later" side during its own discovery pass, potentially enqueuing
  the relationship suggestion in both directions (the dedup index keys on
  `(kind, decision_id, target_id)`, which does not catch a reversed pair).
- **No relationship taxonomy beyond
  supersedes/supplements/conflicts/unrelated.** A richer taxonomy (e.g.
  distinguishing *why* two decisions conflict) is not part of v1.
