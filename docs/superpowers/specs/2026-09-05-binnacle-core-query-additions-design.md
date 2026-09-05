# binnacle-core query additions — design spec

Status: proposed (pending user review)
Author: Yining Chen, with Claude Opus 5
Date: 2026-09-05

## 1. Context and goals

A use-case review across `binnacle-core`, `binnacle-router`, and
`binnacle-ui` (2026-09-04/05 session) surfaced five gaps in
`binnacle-core`'s **read** surface. Every one traces to a named human
curation journey, not to a speculative "APIs should have this" default:

| Gap | Journey that demands it |
|---|---|
| No sortable ordering | "Show me long-term decisions made 5 years ago that are still `current`, so I can check whether they still hold" — needs oldest-first; reviewing freshly-promoted policy needs newest-first. |
| No pagination | Browsing/searching all decisions in a UI. `relevant()`'s `limit` was designed for *agent context injection* (FR-6.7 — a deliberately small top-N slice), not for a human paging through a filtered set. |
| No `valid_until` range filter | "What expires in the next two weeks?" — so temporary waivers get renewed deliberately instead of silently lapsing. |
| No evidence-ref filter | "Which decisions cite this tradewind session as their justification?" |
| No aggregates | A curation dashboard's landing page (queue counts by kind, stale long-term count, domains with zero decisions). Every existing method returns rows; nothing returns counts. |

These are read-path additions only. The write path, lifecycle engine,
authority rules, and record semantics are untouched.

## 2. Non-goals

- **No write-path or lifecycle change.** Nothing here alters how decisions
  are recorded, promoted, superseded, or archived. Invalidating a stale
  long-term decision uses the existing `supersede()` — no new action, status,
  or transition kind is introduced.
- **No new "reviewed / still valid" marker.** Staleness is *inferred* from
  time since last transition, not tracked by an explicit human "I checked
  this" action. The active-tracking variant was considered and rejected as new
  scope on the transition model for a need the passive version already serves.
- **No real-time conflict detection at record time.** Wanted, but it
  contradicts I-5 (recording never awaits judgment) and NFR-7's write/embed
  decoupling. The async discovery sweep already delivers the same information
  without blocking writes.
- **No `binnacle-router` or `binnacle-ui` work.** Those have their own specs;
  this one is the dependency they build on.

## 3. The additions

### 3.1 Sortable ordering on `relevant()`

```python
sort: Literal["decided_at", "recorded_at", "last_touched_at", "valid_until"] = "recorded_at"
order: Literal["asc", "desc"] = "desc"
```

The defaults reproduce today's shipped behavior exactly —
`postgres_store.py:826` and `:848` already order by `d.recorded_at DESC,
d.decision_id ASC`. This addition **documents and parameterizes an existing
ordering; it does not introduce or change one.**

`last_touched_at` is derived — `MAX(transitions.at)` per decision — and is the
staleness signal the long-term review journey needs. Long-term decisions never
auto-archive (FR-3.4 applies to short-term only), so nothing in the record
currently surfaces "untouched the longest."

**Why not just sort by `recorded_at`, which is stored and needs no join?**
Because it answers a different question, and answers the staleness one wrongly.
Supplementing a decision writes a **two-sided** `supplement_linked` transition
pair — one on each side of the link
(`application/lifecycle.py:820-825`, verified). So a 2021 policy that was
supplemented in 2025 carries a 2025 transition: `last_touched_at` correctly
reports it as recently engaged with, while `recorded_at` would rank it as the
single stalest decision in the record. `recorded_at` is wrong precisely for
well-maintained decisions — the ones least deserving of a staleness flag. This
distinction is why the sort key exists, and it is worth restating because
`last_touched_at` is the sole reason for the breaking change in §3.2.

Parameterized ordering already has precedent in this codebase:
`open_queue()` (`postgres_store.py:990`) maps `oldest`/`shakiest`/`domain` to
three different `ORDER BY` clauses. The new sort keys follow that pattern
rather than inventing one.

The sort key set is **closed**. Arbitrary column ordering is not supported —
a closed set is testable, and each member can be reasoned about for index
support (§5).

### 3.2 Keyset pagination on `relevant()` and `queue()`

`relevant()`'s return type changes from a bare list to a page envelope:

```python
@dataclass(frozen=True)
class Page[T]:
    items: list[T]
    next_cursor: str | None  # opaque; None when the page is the last one
```

**Corrected 2026-09-05 (as-built).** This spec originally proposed
`class Page[T](BaseModel)`. Reading `domain/models.py` during planning showed
every *read* model in this package is a frozen dataclass — `CompactDecision`,
`DomainRecord`, `HistoryRecord`, `QueueItemView` — while pydantic is reserved
for *input* models (`Ref`, `OptionConsidered`, `NewDecision`). `Page` follows
the read-model convention (GUIDELINES §13.5), and this block is corrected to
match what shipped rather than left describing a proposal (§5.3: code is
authoritative for existence).

Callers resume with `after: str | None = None`. The cursor is **opaque to the
caller and encoded by `binnacle-core`** — a base64 payload carrying the sort
key value, the tiebreaker id, and the `(sort, order)` it was minted under.

Opaqueness is deliberate and load-bearing: a *typed* cursor
(`Cursor(recorded_at, decision_id)`) would pin the pagination mechanism into
the public API. A survey of other stores confirms the cost of that — keyset
cursors (Postgres, MySQL, SQLite, Mongo, Elasticsearch `search_after`) carry a
sort value plus tiebreaker, while backend-supplied tokens (DynamoDB
`LastEvaluatedKey`, Cassandra paging state) are key maps or opaque blobs of an
entirely different shape. A `str` accommodates both; a typed cursor would make
adopting such a backend a breaking API change.

~~**Verify at plan time:** pydantic v2's handling of PEP 695 generic
syntax.~~ **Moot (2026-09-05):** `Page` is a frozen dataclass, not a pydantic
model (see the correction above), so pydantic's generic handling never enters
the picture. PEP 695 syntax on a dataclass is native to Python ≥3.13, which
this package already requires.

**Why an envelope, when an input-only cursor would have been non-breaking:**
because an input-only cursor does not actually work. It requires the caller to
construct the next cursor from the last row it received, and `last_touched_at`
is a *derived* value that is not a field on `Decision` — a caller sorting by it
cannot see it in the rows it got back. Returning the cursor is the only design
that supports the derived sort key the staleness journey depends on.

**Cursors are order-scoped.** A cursor minted under one `(sort, order)` pair is
rejected with a typed error if replayed under a different one, rather than
silently returning wrong results. This matters most for `queue()`, whose
`shakiest` mode is a three-column composite
(`COALESCE(q.confidence, d.confidence, 1.0) ASC, q.proposed_at ASC, q.item_id ASC`,
`postgres_store.py:990`) — its cursor must carry all three components.

**Predicate shape.** `relevant()`'s existing tiebreaker runs *opposite* to its
primary sort (`recorded_at DESC, decision_id ASC`), so the compact SQL row
comparison `(a, b) < (x, y)` — which requires uniform direction — cannot be
used. The keyset predicate is written explicitly instead:

```sql
WHERE (d.recorded_at < %(cursor_ts)s)
   OR (d.recorded_at = %(cursor_ts)s AND d.decision_id > %(cursor_id)s)
```

The alternative — flipping the tiebreaker to `decision_id DESC` so row
comparison works — was rejected: it would change result ordering for
same-timestamp rows, a real behavior change, to buy SQL tidiness worth nothing
at this scale.

**Pagination is stable against inserts, not against a moving "now".** With no
`as_of`, FR-6.1 assumes the current instant, so page 1 fetched at 10:00 and
page 2 at 10:05 filter against different instants — a decision whose
`valid_until` falls between them shifts the result set under the cursor.
`binnacle-core` does **not** silently freeze an instant into the cursor:
returning an hour-stale view from page 2 is its own surprise, and hidden state
contradicts FR-8.1's "no hidden state" stance. **The remedy is the caller's and
is one parameter: pass an explicit `as_of` to pin the view for the duration of
a pagination run.** Documented here with the fix named, not merely the hazard —
a warning without a remedy just relocates the trap.

### 3.3 `changes()` gains `after_id`

```python
after_id: int | None = None  # transition_id, exclusive
```

`changes()` already has a client-visible cursor in its `since` parameter, and
already orders `t.at DESC, t.transition_id DESC` (`postgres_store.py:943`) —
uniform direction, unlike `relevant()`. It needs only a tiebreaker for
transitions sharing a timestamp, not a full cursor type. Transitions grow
unbounded forever (archival bounds decisions, never the transition log), so
this is the one feed where deep paging is genuinely inevitable.

### 3.4 Evidence-ref filter on `relevant()`

```python
evidence: tuple[str, str] | None = None  # (kind, identifier), exact match
```

Semantics differ deliberately from the existing `subject` filter. `subject`
matches "decisions scoped to X **or** unscoped" (FR-6.1) — the fallback is
meaningful there, since an unscoped decision genuinely governs X. Evidence has
no such fallback: "cites session Y" is an exact-match question, and folding in
decisions that cite nothing would be nonsense.

### 3.5 Expiry filter on `relevant()`

```python
expiring_before: datetime | None = None  # valid_until < X, and valid_until IS NOT NULL
```

Combined with `sort="valid_until", order="asc"`, this answers "what expires
soonest" directly — no separate aggregate needed.

### 3.6 Two aggregate methods

```python
async def queue_summary(self, domains: list[str] | None = None) -> dict[str, int]
async def domain_summary(self) -> list[DomainSummary]
```

`DomainSummary` is a new domain model (`name`, `description`, `active`,
`decision_count`), re-exported from `binnacle_core/__init__.py` alongside the
existing `DomainRecord`.

`domain_summary()` uses a `LEFT JOIN` from `domains`, not a `GROUP BY` over
`decisions` — a plain grouping silently omits zero-decision domains, which are
exactly the rows the registry-housekeeping use case is looking for.

Two small single-purpose methods, not one `dashboard()` call: a combined method
would need "and" to describe it (GUIDELINES §8), and the other dashboard tiles
("5 stalest", "5 expiring soonest") need no aggregate at all — they are
`relevant()` calls with the new sort keys and `limit=5`.

### 3.7 Total count as a separate call

```python
async def relevant_count(self, ...same filters as relevant()...) -> int
```

`Page` deliberately carries **no** total count. Keyset pagination cannot
produce one cheaply — it would require a second `COUNT(*)` over the whole
filtered set on *every* page fetch, paid whether the caller wants it or not.

Instead the count is its own call, made once and cached by the caller for as
long as the filter set holds. It accepts exactly `relevant()`'s **filter**
parameters (`domains`, `subject`, `status`, `tier`, `as_of`,
`include_archived`, `evidence`, `expiring_before`, lexical text) and none of
its presentation parameters (`sort`, `order`, `after`, `limit`, `projection`),
which cannot affect a count.

No equivalent is needed for `queue()`: `queue_summary()` (§3.6) already returns
counts by kind, and their sum is the queue total.

A cached count drifts as decisions are recorded or archived concurrently. That
is acceptable — it is a UI affordance ("about 1,240 results"), not a value any
caller should treat as transactionally consistent with the page in hand.

**Guarding against filter drift between the two methods.** `relevant()` and
`relevant_count()` must accept and apply the same filters forever; if a future
change adds one to `relevant()` and forgets `relevant_count()`, every caller
using them together gets counts that silently disagree with their pages,
through no fault of their own and with no symptom until someone notices the
totals are wrong. Name-drift and wiring-drift are different failures, so three
layers, each cheap and none redundant:

1. **One shared WHERE-clause builder in the store.** Both methods translate
   filters to SQL through the same internal function rather than each
   assembling conditions. This makes "present on both but *applied*
   differently" hard to write. (The public signatures stay loose kwargs — a
   shared `DecisionFilter` parameter object would make drift structurally
   impossible, but costs a second breaking change to `relevant()`'s call style
   and worse ergonomics for every caller.)
2. **A signature test** (unit, no database), which catches a newly-added but
   unforwarded parameter the moment it appears:

```python
def test_relevant_count_accepts_every_relevant_filter() -> None:
    """relevant_count() must accept exactly relevant()'s filter parameters.
    If they drift, counts silently disagree with the pages they describe."""
    presentation = {"self", "sort", "order", "after", "limit", "projection"}
    relevant_filters = set(inspect.signature(Binnacle.relevant).parameters) - presentation
    count_filters = set(inspect.signature(Binnacle.relevant_count).parameters) - {"self"}
    assert relevant_filters == count_filters
```

3. **One integration test of the actual invariant** (`tests/db/`): that
   `relevant_count(F)` equals the number of rows obtained by paging all the way
   through `relevant(F)`. This is what proves the two agree in *behavior*
   rather than merely in parameter names.

## 4. Public surface changes

Additive, except where noted:

- `relevant()` — five new parameters; **return type changes** to `Page[...]`
  (breaking, §7 DR-1).
- `queue()` — `after` parameter; return type changes to `Page[QueueItemView]`
  (breaking). Settled (§9 OQ-2): taken now, alongside `relevant()`'s, because a
  second breaking change later would cost far more than this one does today.
- `changes()` — `after_id` parameter (additive; no return change).
- `queue_summary()`, `domain_summary()`, `relevant_count()` — new methods.
- `Page`, `DomainSummary` — new types, re-exported from the top-level package
  per its "deliberately narrow public surface" contract
  (`binnacle_core/__init__.py`).

`relevant()`'s existing `@overload` set (which narrows the return type by
`projection`, guarded by `tests/unit/test_typing_narrowing.py`) must be updated
to narrow to `Page[Decision]` / `Page[CompactDecision]`. That test is the
existing guard for this behavior and extends to cover the new shape.

### 4.1 Document amendments this triggers

GUIDELINES §5 requires REQUIREMENTS/ARCHITECTURE to move in the same commit as
the behavior they describe, and §5.1 requires every schema-describing file to
move together. This change touches:

- **REQUIREMENTS.md FR-6.1** (Relevance) — amended: sort/order, pagination,
  the evidence filter, and `expiring_before`.
- **REQUIREMENTS.md FR-6.4** (Queue reads) — amended: pagination.
- **REQUIREMENTS.md FR-6.5** (Changes feed) — amended: `after_id`.
- **REQUIREMENTS.md FR-6.10** — **new**: aggregates and counts
  (`queue_summary()`, `domain_summary()`, `relevant_count()`). FR-6.9 is
  currently the last in the series.
- **REQUIREMENTS.md NFR-7** — new rows in the performance table (§6).
- **ARCHITECTURE.md §3** — the Query Service component row gains the aggregate
  responsibility.
- **ARCHITECTURE.md §4** — the schema block gains the new index (§5).
- **`packages/binnacle-core/CHANGELOG.md`** — `Unreleased` entry, with the
  breaking return-type change called out.
- **`docs/PROJECT.md`** — delivery status entries, each naming its package.

## 5. Schema and index impact

No table or column changes. Index needs, per addition:

- **Evidence filter** — `idx_refs_subject` is partial (`WHERE role = 'subject'`)
  and cannot serve evidence lookups. Needs a sibling partial index on
  `refs(kind, identifier) WHERE role = 'evidence'`. One migration, numbered
  **0004** (0001–0003 exist), shipping both an apply step and a rollback step
  per ARCHITECTURE §4.1's requirement that every migration carry a down-path.
- **`last_touched_at` sort** — needs `MAX(transitions.at)` per decision.
  `idx_trans_decision` (added in migration 0003) already leads with
  `decision_id`, which is what this lookup requires. **No new index assumed
  until measured** (§5.4): the perf test in §6 decides.
- **`valid_until` filter/sort** — no index proposed initially, same reasoning.

## 6. Performance (NFR-7)

The existing seeded perf test (10k decisions / 100k transitions,
`tests/db/test_perf.py`) is extended to cover the new query shapes, since
NFR-7's targets bind to measured evidence, not assumption (GUIDELINES §5.4).
`relevant()`'s existing target (< 200 ms p95) applies unchanged to the new
sorts and filters; a sort or filter that cannot meet it under measurement is
what justifies adding an index from §5 — not a guess made while writing this
document.

The three new methods need their own NFR-7 rows, since an operation with no
target cannot fail a performance review. Proposed, to be **validated by
measurement rather than accepted as written**:

| Operation | Proposed target (p95) |
|---|---|
| `relevant_count()` | < 200 ms — same filtered scan as `relevant()`, without hydration |
| `queue_summary()` | < 100 ms — one `GROUP BY` over open items, bounded by `idx_queue_open` |
| `domain_summary()` | < 100 ms — one `LEFT JOIN` over the registry, which is small by construction |

A method that misses its proposed target under the seeded harness gets an
index or a revised target, decided from the measurement — not from this table.

**One measurement not about this spec's additions**, captured here because the
seeded harness is the only place that builds design-scale data: record the
`export()` bundle's **size and duration** at 10k/100k. The
`binnacle-router` spec defers its "should `/export` stream?" decision to that
number, and the router's own test suite has no seeded corpus of its own. The
marginal cost is a few lines in a perf test already being modified.

## 7. Decision records

- **DR-1 Pagination returns an envelope, accepting a breaking change.** An
  input-only cursor (non-breaking) cannot support `last_touched_at`, because
  the caller cannot see a derived value in its returned rows. `binnacle-core` is
  0.3.0, pre-1.0, and its only consumers are its own tests and an unbuilt
  router — this break costs nearly nothing now and grows more expensive with
  every consumer added later.
- **DR-2 Closed sort-key set.** Four named keys, not arbitrary column names:
  each can be tested and index-reasoned; arbitrary ordering invites unindexed
  scans that NFR-7 would not catch until production.
- **DR-3 Cursors are order-scoped and rejected on mismatch.** Replaying a
  cursor under a different `(sort, order)` returns silently wrong results
  otherwise — the failure mode is invisible, so it must be refused loudly.
- **DR-4 `relevant()`'s mixed-direction tiebreaker is preserved.** Writing the
  explicit two-clause keyset predicate is preferred over flipping
  `decision_id` to `DESC` for tidier SQL, because the flip changes existing
  result ordering on tied timestamps.
- **DR-5 `changes()` gets a tiebreaker, not a cursor type.** Its `since`
  parameter is already a client-visible cursor and its ordering is already
  uniform-direction; a full cursor type would be ceremony for no gain.
- **DR-6 Staleness is inferred, not tracked.** No "reviewed / still valid"
  transition action is added. `last_touched_at` answers the journey's actual
  question ("has anything happened to this in years?") without extending the
  transition model.
- **DR-7 Total count is a separate call, not a `Page` field.** Embedding it
  would tax every page fetch with a second `COUNT(*)` over the full filtered
  set, whether or not the caller wants it. As its own call it is paid once per
  filter set and cached by the caller, and callers that never display a count
  never pay for one.

## 8. Versioning

`0.3.0` → `0.4.0`, carrying a `BREAKING CHANGE:` footer for the `relevant()` /
`queue()` return-type change (GUIDELINES §11 permits a pre-1.0 breaking change
to ride a minor bump when called out explicitly). **Proposed, not applied** —
§11 requires explicit confirmation before any bump.

## 9. Open questions

- **OQ-1** ~~Does `Page` need a total count?~~ **Resolved (2026-09-05):** no.
  The count is a separate call, `relevant_count()` (§3.7), made once per filter
  set and cached by the caller. Embedding it in `Page` would charge every page
  fetch for a second `COUNT(*)` that most fetches don't need. `binnacle-ui`
  renders "load more" rather than numbered pages.
- **OQ-2** ~~Should `queue()`'s return type change too?~~ **Resolved
  (2026-09-05):** yes — `queue()` returns `Page[QueueItemView]`, taken in the
  same breaking change as `relevant()`. The decisive argument is timing rather
  than symmetry: the marginal cost now is near zero, while discovering later
  that the review-queue UI needs paging would mean a second breaking change
  against however many consumers exist by then.

None outstanding.
