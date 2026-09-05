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
class Page[T](BaseModel):
    items: list[T]
    next_cursor: str | None  # opaque; None when the page is the last one
```

Callers resume with `after: str | None = None`. The cursor is **opaque to the
caller and encoded by `binnacle-core`** — a base64 payload carrying the sort
key value, the tiebreaker id, and the `(sort, order)` it was minted under.

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

## 4. Public surface changes

Additive, except where noted:

- `relevant()` — five new parameters; **return type changes** to `Page[...]`
  (breaking, §7 DR-1).
- `queue()` — `after` parameter; return type changes to `Page[QueueItemView]`
  for consistency (breaking). This one is *proposed rather than settled* —
  see §9, which weighs it against leaving `queue()` on a bare list.
- `changes()` — `after_id` parameter (additive; no return change).
- `queue_summary()`, `domain_summary()`, `Page`, `DomainSummary` — new,
  re-exported from the top-level package per its "deliberately narrow public
  surface" contract (`binnacle_core/__init__.py`).

`relevant()`'s existing `@overload` set (which narrows the return type by
`projection`, guarded by `tests/unit/test_typing_narrowing.py`) must be updated
to narrow to `Page[Decision]` / `Page[CompactDecision]`. That test is the
existing guard for this behavior and extends to cover the new shape.

## 5. Schema and index impact

No table or column changes. Index needs, per addition:

- **Evidence filter** — `idx_refs_subject` is partial (`WHERE role = 'subject'`)
  and cannot serve evidence lookups. Needs a sibling partial index on
  `refs(kind, identifier) WHERE role = 'evidence'`. One migration.
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

## 8. Versioning

`0.3.0` → `0.4.0`, carrying a `BREAKING CHANGE:` footer for the `relevant()` /
`queue()` return-type change (GUIDELINES §11 permits a pre-1.0 breaking change
to ride a minor bump when called out explicitly). **Proposed, not applied** —
§11 requires explicit confirmation before any bump.

## 9. Open questions

- **Does `Page` need a total count?** A UI showing "page 3 of 47" needs one;
  keyset pagination cannot produce it cheaply (it requires a second `COUNT(*)`
  over the full filtered set). Recommendation: omit it, and have
  `binnacle-ui` render "load more" rather than numbered pages — but this is a
  UX call worth making deliberately rather than by omission.
- **Should `queue()`'s return type change too, or only `relevant()`'s?** This
  spec proposes both for consistency, at the cost of a second breaking change.
  Keeping `queue()` on a bare list is defensible if its result sets are
  expected to stay small (open queue items are bounded by human attention in
  practice).
