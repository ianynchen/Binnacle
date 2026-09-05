# binnacle-core query additions — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add sortable ordering, keyset pagination, two new filters, and three aggregate/count methods to `binnacle-core`'s read surface, so a human-facing UI can browse and search the decision record.

**Architecture:** All changes are read-path. A shared WHERE-clause builder is extracted from `PostgresStore.relevant()` so filtering logic has one home; ordering and keyset predicates are built beside it; `relevant()` and `queue()` change from returning bare lists to a `Page` envelope carrying an opaque cursor. No write path, lifecycle, or schema-semantics change.

**Tech Stack:** Python ≥3.13, async psycopg3, pydantic v2 (input models only — read models are frozen dataclasses), yoyo-migrations, pytest, mypy strict, import-linter.

**Spec:** [docs/superpowers/specs/2026-09-05-binnacle-core-query-additions-design.md](../specs/2026-09-05-binnacle-core-query-additions-design.md)

## Global Constraints

- **Read models are `@dataclass(frozen=True)`, not pydantic `BaseModel`.** `CompactDecision`, `DomainRecord`, `HistoryRecord`, `QueueItemView` are all frozen dataclasses; only input models (`Ref`, `OptionConsidered`, `NewDecision`) are pydantic. `Page` and `DomainSummary` follow the dataclass convention (GUIDELINES §13.5).
- **`relevant()` is already near mypy's overload-resolution limit.** Its docstring records a real prior failure: with this many `Optional` parameters, passing a union-typed argument at a call site makes mypy give up with "Not all union combinations were tried." The existing workaround — dispatching to `self._store.relevant(...)` in two branches, each passing a *literal* `compact_chars` rather than a variable — **must be preserved**, and every new parameter added here raises the risk. If mypy reports that error, do not widen types to silence it; keep the literal-per-branch dispatch and split the branch further.
- **The sort key set is closed:** `decided_at`, `recorded_at`, `last_touched_at`, `valid_until`. No arbitrary column ordering.
- **Cursors are order-scoped.** A cursor minted under one `(sort, order)` is rejected with `InvalidCursor` if replayed under another — never silently honored.
- **`relevant()`'s existing ordering is preserved as the default:** `recorded_at DESC, decision_id ASC`. The mixed direction is deliberate; do not "tidy" the tiebreaker to `DESC`.
- **Every migration ships an apply step and a rollback step**, using the `{schema}` placeholder (ARCHITECTURE §4.1).
- Version bump `0.3.0` → `0.4.0` with a `BREAKING CHANGE:` footer — **proposed and confirmed with the user before applying, never silently** (GUIDELINES §11).
- ruff line-length 100; mypy strict; `bash scripts/check.sh` is the full gate.

---

## File Structure

**Create:**
- `packages/binnacle-core/src/binnacle_core/application/cursors.py` — cursor encode/decode. One responsibility: turning `(sort, order, value, tiebreaker)` into an opaque string and back. Stdlib only, so it stays inside import-linter's "application is driver-free" contract.
- `packages/binnacle-core/src/binnacle_core/migrations/0004_evidence_ref_index.sql` (+ `.rollback.sql`)
- `packages/binnacle-core/tests/unit/test_cursors.py`
- `packages/binnacle-core/tests/unit/test_query_signatures.py`
- `packages/binnacle-core/tests/db/test_pagination.py`
- `packages/binnacle-core/tests/db/test_aggregates.py`

**Modify:**
- `domain/models.py` — add `Page`, `DomainSummary`
- `domain/errors.py` — add `InvalidCursor`
- `__init__.py` — re-export the three new names
- `application/ports.py` — `StorePort` signatures
- `adapters/postgres_store.py` — WHERE/ORDER builders, keyset, count, aggregates
- `client.py` — public signatures and overloads
- `tests/unit/test_typing_narrowing.py`, `tests/db/test_query.py`, `tests/db/test_client.py`, `tests/db/test_narrative_e2e.py`, `tests/db/test_perf.py` — call-site updates
- `docs/binnacle-core/REQUIREMENTS.md`, `docs/binnacle-core/ARCHITECTURE.md`, `packages/binnacle-core/CHANGELOG.md`, `docs/PROJECT.md`

---

### Task 1: `Page`, `DomainSummary`, `InvalidCursor`, and the cursor codec

**Files:**
- Create: `packages/binnacle-core/src/binnacle_core/application/cursors.py`
- Create: `packages/binnacle-core/tests/unit/test_cursors.py`
- Modify: `packages/binnacle-core/src/binnacle_core/domain/models.py`
- Modify: `packages/binnacle-core/src/binnacle_core/domain/errors.py`
- Modify: `packages/binnacle-core/src/binnacle_core/__init__.py`

**Interfaces:**
- Produces: `Page[T](items: list[T], next_cursor: str | None)`; `DomainSummary(name, description, active, decision_count)`; `InvalidCursor(BinnacleError)`; `encode_cursor(*, sort: str, order: str, value: datetime | None, tiebreaker: str) -> str`; `decode_cursor(cursor: str, *, sort: str, order: str) -> tuple[datetime | None, str]`.

- [ ] **Step 1: Write the failing tests**

```python
# packages/binnacle-core/tests/unit/test_cursors.py
"""A cursor must survive a round trip and must refuse to be replayed under a
different ordering -- replaying it silently would return a page computed
against the wrong sort, a wrongness with no visible symptom."""

import string
from datetime import UTC, datetime

import pytest

from binnacle_core import InvalidCursor
from binnacle_core.application.cursors import decode_cursor, encode_cursor


def test_round_trips_value_and_tiebreaker() -> None:
    at = datetime(2021, 3, 14, 9, 22, 11, tzinfo=UTC)
    token = encode_cursor(sort="recorded_at", order="desc", value=at, tiebreaker="abc")
    assert decode_cursor(token, sort="recorded_at", order="desc") == (at, "abc")


def test_round_trips_a_null_sort_value() -> None:
    token = encode_cursor(sort="valid_until", order="asc", value=None, tiebreaker="abc")
    assert decode_cursor(token, sort="valid_until", order="asc") == (None, "abc")


def test_rejects_a_cursor_minted_under_a_different_sort() -> None:
    token = encode_cursor(sort="recorded_at", order="desc", value=None, tiebreaker="abc")
    with pytest.raises(InvalidCursor):
        decode_cursor(token, sort="decided_at", order="desc")


def test_rejects_a_cursor_minted_under_a_different_direction() -> None:
    token = encode_cursor(sort="recorded_at", order="desc", value=None, tiebreaker="abc")
    with pytest.raises(InvalidCursor):
        decode_cursor(token, sort="recorded_at", order="asc")


def test_rejects_a_malformed_cursor_rather_than_falling_back_to_page_one() -> None:
    with pytest.raises(InvalidCursor):
        decode_cursor("not-base64-at-all!!", sort="recorded_at", order="desc")


def test_cursor_is_url_safe_so_it_survives_a_query_string() -> None:
    """binnacle-router will carry this in a URL, so the alphabet matters:
    '+' and '/' from standard base64 would need escaping."""
    token = encode_cursor(sort="recorded_at", order="desc", value=None, tiebreaker="abc")
    assert set(token) <= set(string.ascii_letters + string.digits + "-_")
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest -c packages/binnacle-core/pyproject.toml packages/binnacle-core/tests/unit/test_cursors.py -q`
Expected: FAIL — `ModuleNotFoundError: binnacle_core.application.cursors`

- [ ] **Step 3: Add `InvalidCursor` to `domain/errors.py`**

Append, following the file's existing error-class style:

```python
class InvalidCursor(BinnacleError):
    """A pagination cursor is malformed, or was minted under a different
    (sort, order) than the query replaying it. Refused rather than honored:
    silently paging under the wrong ordering returns wrong rows with no
    visible symptom."""
```

- [ ] **Step 4: Add `Page` and `DomainSummary` to `domain/models.py`**

Both are frozen dataclasses, matching every other read model in this file:

```python
@dataclass(frozen=True)
class Page[T]:
    """One page of a keyset-paginated read. `next_cursor` is opaque: pass it
    back as `after=` to fetch the following page, or `None` when this page is
    the last. Carries no total count -- see `Binnacle.relevant_count()`."""

    items: list[T]
    next_cursor: str | None


@dataclass(frozen=True)
class DomainSummary:
    """One registry row with its decision count (FR-6.10). Domains with no
    decisions are included, with `decision_count == 0`."""

    name: str
    description: str
    active: bool
    decision_count: int
```

- [ ] **Step 5: Write `application/cursors.py`**

```python
"""Opaque pagination cursors.

The wire form is deliberately an opaque string rather than a typed value:
keyset cursors (this store) carry a sort value plus tiebreaker, while a
different backend's cursor could be a key map or a driver-supplied blob. A
string accommodates both; a typed cursor would make such a change breaking.
"""

import base64
import binascii
import json
from datetime import datetime

from binnacle_core.domain.errors import InvalidCursor


def encode_cursor(*, sort: str, order: str, value: datetime | None, tiebreaker: str) -> str:
    """Mint a cursor for the last row of a page. `value` is that row's sort-key
    value, `tiebreaker` its id (as `str`)."""
    payload = {
        "s": sort,
        "o": order,
        "v": value.isoformat() if value is not None else None,
        "t": tiebreaker,
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str, *, sort: str, order: str) -> tuple[datetime | None, str]:
    """Reverse of `encode_cursor`, refusing a cursor minted under a different
    ordering. Raises `InvalidCursor` on malformed input or a mismatch."""
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()))
    except (ValueError, binascii.Error) as exc:
        raise InvalidCursor(f"cursor is not decodable: {cursor[:32]!r}") from exc
    if not isinstance(payload, dict):
        raise InvalidCursor(f"cursor payload is not an object: {cursor[:32]!r}")
    if payload.get("s") != sort or payload.get("o") != order:
        raise InvalidCursor(
            f"cursor was minted for sort={payload.get('s')!r} order={payload.get('o')!r}, "
            f"replayed under sort={sort!r} order={order!r}"
        )
    raw_value = payload.get("v")
    value = datetime.fromisoformat(raw_value) if isinstance(raw_value, str) else None
    tiebreaker = payload.get("t")
    if not isinstance(tiebreaker, str):
        raise InvalidCursor(f"cursor carries no tiebreaker: {cursor[:32]!r}")
    return value, tiebreaker
```

- [ ] **Step 6: Re-export from `__init__.py`**

Add `DomainSummary` and `Page` to the `from binnacle_core.domain.models import (...)` block, `InvalidCursor` to the `domain.errors` block, and all three to `__all__` — keeping both lists alphabetically sorted, as they already are.

- [ ] **Step 7: Run the tests and the type gate**

Run: `uv run pytest -c packages/binnacle-core/pyproject.toml packages/binnacle-core/tests/unit/test_cursors.py -q`
Expected: 6 passed

Run: `uv run mypy --config-file packages/binnacle-core/pyproject.toml packages/binnacle-core/src`
Expected: `Success: no issues found`

- [ ] **Step 8: Commit**

```bash
git add packages/binnacle-core/src/binnacle_core packages/binnacle-core/tests/unit/test_cursors.py
git commit -m "feat(binnacle-core): add Page, DomainSummary, and the cursor codec

Cursors are opaque strings and order-scoped: replaying one under a
different (sort, order) raises InvalidCursor rather than silently
returning rows computed against the wrong ordering.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: Extract the shared WHERE-clause builder (pure refactor)

**Files:**
- Modify: `packages/binnacle-core/src/binnacle_core/adapters/postgres_store.py` (the `relevant()` body)

**Interfaces:**
- Produces: `PostgresStore._relevant_where(...) -> tuple[str, dict[str, Any]]`, consumed by `relevant()` now and by `relevant_count()` in Task 3.

No behavior change: this task is green if the existing suite passes untouched.

- [ ] **Step 1: Extract the method**

Add to `PostgresStore`, lifting the condition/param construction verbatim from `relevant()`:

```python
def _relevant_where(
    self,
    *,
    domains: Sequence[str] | None,
    status: Sequence[str] | None,
    tier: Tier | None,
    subject: tuple[str, str] | None,
    as_of: datetime | None,
    text: str | None,
    include_archived: bool,
) -> tuple[str, dict[str, Any]]:
    """The FR-6.1 filter set as SQL. Shared verbatim by `relevant()` and
    `relevant_count()` so the two can never disagree about what a filter
    means -- a divergence would make counts silently contradict the pages
    they describe."""
    schema = self._schema
    statuses = set(status) if status is not None else set(_DEFAULT_RELEVANT_STATUS)
    if include_archived:
        statuses.add("archived")
    effective_as_of = as_of if as_of is not None else datetime.now(UTC)

    conditions = ["d.status = ANY(%(statuses)s)"]
    params: dict[str, Any] = {"statuses": list(statuses), "as_of": effective_as_of}
    conditions.append("(d.valid_from IS NULL OR d.valid_from <= %(as_of)s)")
    conditions.append("(d.valid_until IS NULL OR d.valid_until > %(as_of)s)")
    if domains is not None:
        conditions.append("d.domain = ANY(%(domains)s)")
        params["domains"] = list(domains)
    if tier is not None:
        conditions.append("d.tier = %(tier)s")
        params["tier"] = tier
    if subject is not None:
        subj_kind, subj_id = subject
        conditions.append(
            f"(EXISTS (SELECT 1 FROM {schema}.refs r WHERE r.decision_id = d.decision_id "
            "AND r.role = 'subject' AND r.kind = %(subj_kind)s AND r.identifier = %(subj_id)s) "
            f"OR NOT EXISTS (SELECT 1 FROM {schema}.refs r2 "
            "WHERE r2.decision_id = d.decision_id AND r2.role = 'subject'))"
        )
        params["subj_kind"] = subj_kind
        params["subj_id"] = subj_id
    if text is not None:
        conditions.append(
            "(d.scenario ILIKE %(text)s OR d.outcome ILIKE %(text)s OR d.reasoning ILIKE %(text)s)"
        )
        params["text"] = f"%{_escape_ilike(text)}%"
    return " AND ".join(conditions), params
```

- [ ] **Step 2: Rewrite `relevant()`'s opening to call it**

Replace everything in `relevant()` from `schema = self._schema` down to `where_sql = " AND ".join(conditions)` with:

```python
        schema = self._schema
        where_sql, params = self._relevant_where(
            domains=domains,
            status=status,
            tier=tier,
            subject=subject,
            as_of=as_of,
            text=text,
            include_archived=include_archived,
        )
        params["limit"] = limit
```

- [ ] **Step 3: Run the full suite — this must be a no-op**

Run: `uv run pytest -c packages/binnacle-core/pyproject.toml packages/binnacle-core/tests -q`
Expected: same pass count as before the change, zero failures. Any diff here means the extraction changed behavior.

- [ ] **Step 4: Commit**

```bash
git add packages/binnacle-core/src/binnacle_core/adapters/postgres_store.py
git commit -m "refactor(binnacle-core): extract the relevant() WHERE builder

No behavior change. relevant_count() will share this builder so the two
can never disagree about what a filter means.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: `relevant_count()`

**Files:**
- Modify: `application/ports.py`, `adapters/postgres_store.py`, `client.py`
- Create: `packages/binnacle-core/tests/unit/test_query_signatures.py`
- Create: `packages/binnacle-core/tests/db/test_aggregates.py`

**Interfaces:**
- Consumes: `_relevant_where(...)` from Task 2.
- Produces: `Binnacle.relevant_count(...) -> int` and `StorePort.relevant_count(...) -> int`, both taking exactly `relevant()`'s filter parameters (`domains`, `subject`, `status`, `tier`, `as_of`, `text`, `include_archived`) and none of its presentation parameters.

- [ ] **Step 1: Write the failing signature test**

```python
# packages/binnacle-core/tests/unit/test_query_signatures.py
"""relevant() and relevant_count() must accept the same filters forever.

If a filter is added to one and forgotten on the other, counts silently
disagree with the pages they describe -- wrong with no symptom until someone
notices the totals are off. This test is the guard (GUIDELINES §8: rules are
enforced, not aspirational)."""

import inspect

from binnacle_core import Binnacle

PRESENTATION_PARAMS = {"self", "sort", "order", "after", "limit", "projection"}


def test_relevant_count_accepts_every_relevant_filter() -> None:
    relevant_filters = set(inspect.signature(Binnacle.relevant).parameters) - PRESENTATION_PARAMS
    count_filters = set(inspect.signature(Binnacle.relevant_count).parameters) - {"self"}
    assert relevant_filters == count_filters
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest -c packages/binnacle-core/pyproject.toml packages/binnacle-core/tests/unit/test_query_signatures.py -q`
Expected: FAIL — `AttributeError: type object 'Binnacle' has no attribute 'relevant_count'`

- [ ] **Step 3: Add `relevant_count` to `StorePort`**

```python
    async def relevant_count(
        self,
        *,
        domains: Sequence[str] | None = None,
        status: Sequence[str] | None = None,
        tier: Tier | None = None,
        subject: tuple[str, str] | None = None,
        as_of: datetime | None = None,
        text: str | None = None,
        include_archived: bool = False,
    ) -> int:
        """FR-6.10: how many decisions match `relevant()`'s filters. Takes no
        presentation parameters -- sort, cursor, and limit cannot affect a
        count."""
        ...
```

- [ ] **Step 4: Implement it in `PostgresStore`**

```python
    async def relevant_count(
        self,
        *,
        domains: Sequence[str] | None = None,
        status: Sequence[str] | None = None,
        tier: Tier | None = None,
        subject: tuple[str, str] | None = None,
        as_of: datetime | None = None,
        text: str | None = None,
        include_archived: bool = False,
    ) -> int:
        where_sql, params = self._relevant_where(
            domains=domains,
            status=status,
            tier=tier,
            subject=subject,
            as_of=as_of,
            text=text,
            include_archived=include_archived,
        )
        sql = f"SELECT COUNT(*) AS n FROM {self._schema}.decisions d WHERE {where_sql}"
        async with self._read_conn() as conn:
            cur = await conn.execute(sql, params)
            row = await cur.fetchone()
        return int(row["n"]) if row is not None else 0
```

- [ ] **Step 5: Add the client method**

```python
    async def relevant_count(
        self,
        domains: Sequence[str] | None = None,
        subject: tuple[str, str] | None = None,
        status: Sequence[str] = ("current",),
        tier: Tier | None = None,
        as_of: datetime | None = None,
        text: str | None = None,
        include_archived: bool = False,
    ) -> int:
        """FR-6.10: the total matching `relevant()`'s filters, for a caller that
        wants "about N results" alongside a paged read. Deliberately a separate
        call rather than a field on `Page`: embedding it would charge every page
        fetch for a COUNT(*) that most fetches do not need. The value drifts as
        decisions are recorded or archived concurrently -- it is a UI
        affordance, not a figure consistent with the page in hand."""
        return await self._store.relevant_count(
            domains=domains,
            status=status,
            tier=tier,
            subject=subject,
            as_of=as_of,
            text=text,
            include_archived=include_archived,
        )
```

- [ ] **Step 6: Write the behavioral test**

```python
# packages/binnacle-core/tests/db/test_aggregates.py
"""Counts and summaries against a live store."""

import pytest

from tests.helpers import StubEmbedder  # existing test helper


@pytest.mark.asyncio
class TestRelevantCount:
    async def test_count_reflects_the_same_filters_as_relevant(
        self, pg_dsn: str, scratch_schema: str
    ) -> None:
        """A count that ignored a filter would overstate what the caller can
        actually page through."""
        bn = await _seeded_client(pg_dsn, scratch_schema)
        in_arch = await bn.relevant(domains=["architecture"], limit=1000)
        assert await bn.relevant_count(domains=["architecture"]) == len(in_arch)
        assert await bn.relevant_count(domains=["nonexistent"]) == 0
```

Use the same client-construction and seeding helpers the neighbouring
`tests/db/test_query.py` already uses (`_seeded_client` there is the model to
follow; mirror its fixture usage rather than inventing a new one).

- [ ] **Step 7: Run both tests and the gate**

Run: `uv run pytest -c packages/binnacle-core/pyproject.toml packages/binnacle-core/tests/unit/test_query_signatures.py packages/binnacle-core/tests/db/test_aggregates.py -q`
Expected: PASS (db test skips cleanly if `BINNACLE_TEST_DSN` is unreachable)

Run: `uv run mypy --config-file packages/binnacle-core/pyproject.toml packages/binnacle-core/src`
Expected: `Success: no issues found`

- [ ] **Step 8: Commit**

```bash
git add packages/binnacle-core/src/binnacle_core packages/binnacle-core/tests
git commit -m "feat(binnacle-core): add relevant_count()

Shares relevant()'s WHERE builder, and a signature test asserts the two
accept the same filters -- drift between them would make counts silently
contradict the pages they describe.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: Sortable ordering on `relevant()`

**Files:**
- Modify: `adapters/postgres_store.py`, `application/ports.py`, `client.py`
- Modify: `packages/binnacle-core/tests/db/test_query.py`

**Interfaces:**
- Produces: `sort: Literal["decided_at", "recorded_at", "last_touched_at", "valid_until"] = "recorded_at"` and `order: Literal["asc", "desc"] = "desc"` on `StorePort.relevant`, `PostgresStore.relevant`, and `Binnacle.relevant` (all three overloads). Consumed by Task 5's cursor work.

- [ ] **Step 1: Write the failing tests**

Append to `packages/binnacle-core/tests/db/test_query.py`:

```python
@pytest.mark.asyncio
class TestRelevantSorting:
    async def test_defaults_preserve_recorded_at_desc(
        self, pg_dsn: str, scratch_schema: str
    ) -> None:
        """The pre-existing ordering is the default; this addition
        parameterizes it rather than changing it."""
        bn = await _seeded_client(pg_dsn, scratch_schema)
        default = await bn.relevant(limit=50)
        explicit = await bn.relevant(sort="recorded_at", order="desc", limit=50)
        assert [d.id for d in default] == [d.id for d in explicit]

    async def test_oldest_first_reverses_the_default(
        self, pg_dsn: str, scratch_schema: str
    ) -> None:
        bn = await _seeded_client(pg_dsn, scratch_schema)
        newest = await bn.relevant(sort="recorded_at", order="desc", limit=50)
        oldest = await bn.relevant(sort="recorded_at", order="asc", limit=50)
        assert [d.id for d in oldest] == list(reversed([d.id for d in newest]))

    async def test_last_touched_at_ranks_a_supplemented_decision_as_recent(
        self, pg_dsn: str, scratch_schema: str
    ) -> None:
        """The whole point of the derived key: a decision recorded long ago but
        supplemented recently is NOT stale, and recorded_at would rank it
        stalest. supplement() writes a transition on both sides."""
        bn = await _seeded_client(pg_dsn, scratch_schema)
        oldest_by_record = await bn.relevant(sort="recorded_at", order="asc", limit=1)
        target = oldest_by_record[0].id
        newer = await _record_short_term(bn, "a later decision")
        await bn.supplement(newer.decision_id, target, actor=HUMAN)

        by_touch = await bn.relevant(sort="last_touched_at", order="asc", limit=50)
        assert by_touch[0].id != target, "supplementing should stop ranking it stalest"
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest -c packages/binnacle-core/pyproject.toml packages/binnacle-core/tests/db/test_query.py -k Sorting -q`
Expected: FAIL — `TypeError: relevant() got an unexpected keyword argument 'sort'`

- [ ] **Step 3: Add the ORDER BY builder to `PostgresStore`**

```python
_SORT_EXPRESSIONS: dict[str, str] = {
    "decided_at": "d.decided_at",
    "recorded_at": "d.recorded_at",
    "valid_until": "d.valid_until",
    "last_touched_at": "lt.last_touched_at",
}


    def _relevant_order(self, sort: str, order: str) -> tuple[str, str, str]:
        """Returns (join_sql, sort_expression, order_by_sql).

        The tiebreaker stays `d.decision_id ASC` in both directions, preserving
        the ordering that shipped before this parameter existed. Its direction
        deliberately differs from the primary sort's -- see the keyset predicate
        in `_relevant_keyset`, which is written out longhand for that reason.
        """
        expr = _SORT_EXPRESSIONS[sort]
        join_sql = ""
        if sort == "last_touched_at":
            join_sql = (
                f" LEFT JOIN LATERAL (SELECT MAX(t.at) AS last_touched_at "
                f"FROM {self._schema}.transitions t "
                "WHERE t.decision_id = d.decision_id) lt ON TRUE"
            )
        direction = "DESC" if order == "desc" else "ASC"
        return join_sql, expr, f"ORDER BY {expr} {direction}, d.decision_id ASC"
```

- [ ] **Step 4: Wire it through `PostgresStore.relevant()`**

Add `sort: str = "recorded_at"` and `order: str = "desc"` parameters to all three
`relevant` signatures in the file (both overloads and the implementation). In
the body, after the `_relevant_where` call:

```python
        join_sql, _sort_expr, order_sql = self._relevant_order(sort, order)
        if sort == "valid_until":
            where_sql = f"{where_sql} AND d.valid_until IS NOT NULL"
```

Then replace the hardcoded `ORDER BY d.recorded_at DESC, d.decision_id ASC` in
**both** the compact and full branches with `{order_sql}`, and insert
`{join_sql}` immediately after `FROM {schema}.decisions d` in both.

The `valid_until IS NOT NULL` guard is deliberate: sorting by an expiry date is
meaningless for decisions that never expire, and including NULLs would also
break the keyset predicate in Task 5 (`NULL < x` is NULL, not false).
`last_touched_at` needs no such guard — every decision has at least a
`recorded` transition, so `MAX(at)` is never NULL.

- [ ] **Step 5: Mirror the parameters onto `StorePort.relevant` and `Binnacle.relevant`**

Add the same two parameters to `StorePort`'s three `relevant` signatures and to
`Binnacle.relevant`'s three. **Preserve the two-branch dispatch** in
`Binnacle.relevant`'s body — pass `sort=sort, order=order` in both branches
alongside the existing literal `compact_chars`, and do not collapse the
branches (see Global Constraints).

- [ ] **Step 6: Run the tests, the type gate, and the full suite**

Run: `uv run pytest -c packages/binnacle-core/pyproject.toml packages/binnacle-core/tests -q`
Expected: all pass, including the three new sorting tests

Run: `uv run mypy --config-file packages/binnacle-core/pyproject.toml packages/binnacle-core/src packages/binnacle-core/tests/unit/test_typing_narrowing.py`
Expected: `Success`. If it reports "Not all union combinations were tried," see Global Constraints — keep the literal-per-branch dispatch and split further rather than widening types.

- [ ] **Step 7: Commit**

```bash
git add packages/binnacle-core/src/binnacle_core packages/binnacle-core/tests/db/test_query.py
git commit -m "feat(binnacle-core): sortable ordering on relevant()

Four closed sort keys. Defaults reproduce the shipped ordering exactly.
last_touched_at is derived from MAX(transitions.at): a decision recorded
in 2021 but supplemented in 2025 is not stale, and recorded_at would rank
it stalest.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: Keyset pagination — `relevant()` returns `Page` (BREAKING)

**Files:**
- Modify: `adapters/postgres_store.py`, `application/ports.py`, `client.py`
- Modify: `tests/unit/test_typing_narrowing.py`, `tests/db/test_query.py`, `tests/db/test_client.py`, `tests/db/test_narrative_e2e.py`, `tests/db/test_perf.py`
- Create: `packages/binnacle-core/tests/db/test_pagination.py`

**Interfaces:**
- Consumes: `encode_cursor`/`decode_cursor` (Task 1), `_relevant_order` (Task 4), `relevant_count` (Task 3).
- Produces: `Binnacle.relevant(...) -> Page[CompactDecision] | Page[Decision]`, with `after: str | None = None`.

This is the breaking change. Every existing `relevant()` call site gains `.items`.

- [ ] **Step 1: Write the failing tests**

```python
# packages/binnacle-core/tests/db/test_pagination.py
"""Keyset pagination over relevant().

The invariant that matters is not "a cursor round-trips" but "paging through
yields each decision exactly once, and as many as the count promised" --
tested here rather than only in the unit codec tests."""

import pytest


@pytest.mark.asyncio
class TestRelevantPagination:
    async def test_paging_yields_every_decision_exactly_once(
        self, pg_dsn: str, scratch_schema: str
    ) -> None:
        bn = await _seeded_client(pg_dsn, scratch_schema)
        seen: list[str] = []
        cursor: str | None = None
        for _ in range(50):  # generous bound; asserts termination too
            page = await bn.relevant(limit=3, after=cursor)
            seen.extend(str(d.id) for d in page.items)
            cursor = page.next_cursor
            if cursor is None:
                break
        assert cursor is None, "pagination did not terminate"
        assert len(seen) == len(set(seen)), "a decision appeared on two pages"

    async def test_paged_total_equals_relevant_count(
        self, pg_dsn: str, scratch_schema: str
    ) -> None:
        """The count and the pages must describe the same set -- this is what
        the signature test in tests/unit cannot prove."""
        bn = await _seeded_client(pg_dsn, scratch_schema)
        total = await bn.relevant_count(domains=["architecture"])
        seen = 0
        cursor: str | None = None
        while True:
            page = await bn.relevant(domains=["architecture"], limit=2, after=cursor)
            seen += len(page.items)
            cursor = page.next_cursor
            if cursor is None:
                break
        assert seen == total

    async def test_last_page_reports_no_next_cursor(self, pg_dsn: str, scratch_schema: str) -> None:
        bn = await _seeded_client(pg_dsn, scratch_schema)
        page = await bn.relevant(limit=1000)
        assert page.next_cursor is None

    async def test_replaying_a_cursor_under_a_different_sort_is_refused(
        self, pg_dsn: str, scratch_schema: str
    ) -> None:
        from binnacle_core import InvalidCursor

        bn = await _seeded_client(pg_dsn, scratch_schema)
        page = await bn.relevant(sort="recorded_at", order="desc", limit=1)
        assert page.next_cursor is not None
        with pytest.raises(InvalidCursor):
            await bn.relevant(sort="decided_at", order="desc", after=page.next_cursor)
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest -c packages/binnacle-core/pyproject.toml packages/binnacle-core/tests/db/test_pagination.py -q`
Expected: FAIL — `TypeError: relevant() got an unexpected keyword argument 'after'`

- [ ] **Step 3: Add the keyset predicate builder**

```python
def _relevant_keyset(self, *, sort: str, order: str, after: str, params: dict[str, Any]) -> str:
    """The 'resume after this row' condition.

    Written longhand rather than as the compact row comparison
    `(a, b) < (x, y)`, because that form requires both columns to sort in
    the same direction and this ordering's tiebreaker deliberately runs
    opposite to its primary sort.
    """
    value, tiebreaker = decode_cursor(after, sort=sort, order=order)
    expr = _SORT_EXPRESSIONS[sort]
    params["cursor_value"] = value
    params["cursor_id"] = tiebreaker
    comparison = "<" if order == "desc" else ">"
    return (
        f"(({expr} {comparison} %(cursor_value)s) "
        f"OR ({expr} = %(cursor_value)s AND d.decision_id > %(cursor_id)s))"
    )
```

- [ ] **Step 4: Return a `Page` from `PostgresStore.relevant()`**

Add `after: str | None = None` to all three signatures; change the return
annotations to `Page[CompactDecision]` / `Page[Decision]` /
`Page[CompactDecision] | Page[Decision]`. In the body, after the order builder:

```python
if after is not None:
    where_sql = f"{where_sql} AND {
        self._relevant_keyset(sort=sort, order=order, after=after, params=params)
    }"
params["limit"] = limit + 1  # one extra row tells us whether more remain
```

Both branches select the sort expression so a cursor can be minted (add
`, {_sort_expr} AS _sort_value` to the compact branch's column list; the full
branch's `d.*` already carries the stored columns, but `last_touched_at` is
derived, so add `, lt.last_touched_at AS _sort_value` there too when
`join_sql` is non-empty — simplest is to always alias `{_sort_expr} AS
_sort_value` in both branches).

Then, in each branch, trim the extra row and mint the cursor:

```python
        has_more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = (
            encode_cursor(
                sort=sort,
                order=order,
                value=rows[-1]["_sort_value"],
                tiebreaker=str(rows[-1]["decision_id"]),
            )
            if has_more and rows
            else None
        )
```

and wrap each branch's existing list construction in
`Page(items=<the list>, next_cursor=next_cursor)`.

- [ ] **Step 5: Mirror onto `StorePort` and `Binnacle`, and update the overloads**

Add `after: str | None = None` to `StorePort.relevant`'s and
`Binnacle.relevant`'s three signatures each, and change every return annotation
from `list[CompactDecision]` → `Page[CompactDecision]` and `list[Decision]` →
`Page[Decision]`. Keep the two-branch dispatch intact.

- [ ] **Step 6: Update `test_typing_narrowing.py`**

Its `assert_type` calls must now expect the page types:

```python
    assert_type(await bn.relevant(), Page[CompactDecision])
    assert_type(await bn.relevant(projection="full"), Page[Decision])
```

Apply the same substitution to the `StorePort` and `PostgresStore` assertions
in that file, keeping its existing structure and comments.

- [ ] **Step 7: Update every other `relevant()` call site**

Run: `grep -rn "\.relevant(" packages/binnacle-core/tests packages/binnacle-core/src`

For each result that indexes or iterates the return value, add `.items`. This
is mechanical; the compiler and tests will find any missed one.

- [ ] **Step 8: Run everything**

Run: `bash scripts/check.sh`
Expected: all gates pass — ruff, mypy strict, import-linter, full pytest

- [ ] **Step 9: Commit**

```bash
git add packages/binnacle-core
git commit -m "feat(binnacle-core)!: paginate relevant() with keyset cursors

BREAKING CHANGE: relevant() returns Page[...] rather than a bare list.
An input-only cursor would have been non-breaking but cannot work: with
sort=last_touched_at the sort value is derived and absent from the
returned rows, so a caller cannot construct the next cursor from what it
received.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 6: Paginate `queue()` (BREAKING)

**Files:**
- Modify: `adapters/postgres_store.py` (`open_queue`), `application/ports.py`, `client.py`
- Modify: `packages/binnacle-core/tests/db/test_query.py` (existing `queue()` call sites)
- Modify: `packages/binnacle-core/tests/db/test_pagination.py`

**Interfaces:**
- Produces: `Binnacle.queue(kinds=..., order=..., limit: int = 50, after: str | None = None) -> Page[QueueItemView]`.

`open_queue`'s `shakiest` ordering is a three-column composite
(`COALESCE(q.confidence, d.confidence, 1.0) ASC, q.proposed_at ASC, q.item_id ASC`),
so its cursor carries the composite's leading value and `item_id` as tiebreaker.
The cursor's `sort` field is the `order` name (`oldest`/`shakiest`/`domain`), so
replaying a cursor under a different ordering is refused by the same mechanism
as Task 5.

- [ ] **Step 1: Write the failing test**

Append to `test_pagination.py`:

```python
@pytest.mark.asyncio
class TestQueuePagination:
    async def test_paging_the_queue_yields_each_item_once(
        self, pg_dsn: str, scratch_schema: str
    ) -> None:
        bn = await _seeded_client_with_queue(pg_dsn, scratch_schema)
        seen: list[int] = []
        cursor: str | None = None
        while True:
            page = await bn.queue(limit=2, after=cursor)
            seen.extend(v.item.item_id for v in page.items)
            cursor = page.next_cursor
            if cursor is None:
                break
        assert len(seen) == len(set(seen))

    async def test_a_queue_cursor_is_refused_under_a_different_order(
        self, pg_dsn: str, scratch_schema: str
    ) -> None:
        from binnacle_core import InvalidCursor

        bn = await _seeded_client_with_queue(pg_dsn, scratch_schema)
        page = await bn.queue(order="oldest", limit=1)
        assert page.next_cursor is not None
        with pytest.raises(InvalidCursor):
            await bn.queue(order="shakiest", after=page.next_cursor)
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest -c packages/binnacle-core/pyproject.toml packages/binnacle-core/tests/db/test_pagination.py -k Queue -q`
Expected: FAIL — `TypeError: queue() got an unexpected keyword argument 'limit'`

- [ ] **Step 3: Implement in `PostgresStore.open_queue`**

Add `limit: int = 50` and `after: str | None = None`. The existing `order_sql`
map stays; add a parallel map of the leading sort expression per ordering:

```python
_QUEUE_SORT_EXPRESSIONS: dict[str, str] = {
    "oldest": "q.proposed_at",
    "shakiest": "COALESCE(q.confidence, d.confidence, 1.0)",
    "domain": "d.domain",
}
```

Decode with `decode_cursor(after, sort=order, order="asc")` (all three queue
orderings ascend), build the same longhand predicate against the leading
expression with `q.item_id` as tiebreaker, fetch `limit + 1`, trim, and mint
`next_cursor` from the last row's leading value and `item_id`.

`shakiest`'s leading expression is numeric, not a datetime — `encode_cursor`
takes `datetime | None`, so widen its `value` parameter to
`datetime | float | None` and serialize non-datetime values directly (the
decoder already returns whatever was stored; annotate it
`tuple[datetime | float | None, str]` and let callers pass it straight back
into a query parameter).

- [ ] **Step 4: Mirror onto `StorePort.open_queue` and `Binnacle.queue`, return `Page[QueueItemView]`**

- [ ] **Step 5: Update existing `queue()` call sites**

Run: `grep -rn "\.queue(" packages/binnacle-core/tests packages/binnacle-core/src` and add `.items` where the result is indexed or iterated.

- [ ] **Step 6: Run everything**

Run: `bash scripts/check.sh`
Expected: all gates pass

- [ ] **Step 7: Commit**

```bash
git add packages/binnacle-core
git commit -m "feat(binnacle-core)!: paginate queue() with keyset cursors

BREAKING CHANGE: queue() returns Page[QueueItemView]. Taken in the same
release as relevant()'s change rather than later, when a second break
would cost more.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 7: `changes()` gains `after_id`

**Files:**
- Modify: `adapters/postgres_store.py` (`changes`), `application/ports.py`, `client.py`
- Modify: `packages/binnacle-core/tests/db/test_query.py`

**Interfaces:**
- Produces: `Binnacle.changes(..., after_id: int | None = None)`. Return type unchanged — `changes()` keeps its bare list, since `since` is already a client-visible cursor and its ordering (`t.at DESC, t.transition_id DESC`) is uniform-direction.

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
class TestChangesTiebreaker:
    async def test_after_id_excludes_transitions_already_seen(
        self, pg_dsn: str, scratch_schema: str
    ) -> None:
        """Transitions sharing a timestamp would otherwise reappear on the next
        `since=`-based fetch, since `since` alone cannot separate them."""
        bn = await _seeded_client(pg_dsn, scratch_schema)
        first = await bn.changes(limit=5)
        assert first
        last_transition = first[-1][0]
        following = await bn.changes(
            since=last_transition.at, after_id=last_transition.transition_id
        )
        assert all(t.transition_id != last_transition.transition_id for t, _ in following)
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest -c packages/binnacle-core/pyproject.toml packages/binnacle-core/tests/db/test_query.py -k ChangesTiebreaker -q`
Expected: FAIL — `TypeError: changes() got an unexpected keyword argument 'after_id'`

- [ ] **Step 3: Implement**

In `PostgresStore.changes`, add `after_id: int | None = None` and, when set:

```python
        if after_id is not None:
            conditions.append("t.transition_id < %(after_id)s")
            params["after_id"] = after_id
```

`<` rather than `>` because the ordering is `transition_id DESC` — the next
page continues *downward*. Mirror the parameter onto `StorePort.changes` and
`Binnacle.changes`.

- [ ] **Step 4: Run and commit**

Run: `bash scripts/check.sh`
Expected: all gates pass

```bash
git add packages/binnacle-core
git commit -m "feat(binnacle-core): add after_id tiebreaker to changes()

since= alone cannot separate transitions sharing a timestamp, so they
reappear on the next fetch. Additive; changes() keeps its bare list.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 8: Evidence-ref filter and migration 0004

**Files:**
- Create: `packages/binnacle-core/src/binnacle_core/migrations/0004_evidence_ref_index.sql`
- Create: `packages/binnacle-core/src/binnacle_core/migrations/0004_evidence_ref_index.rollback.sql`
- Modify: `adapters/postgres_store.py` (`_relevant_where`), `application/ports.py`, `client.py`
- Modify: `packages/binnacle-core/tests/db/test_query.py`, `tests/db/test_migrations.py`

**Interfaces:**
- Produces: `evidence: tuple[str, str] | None = None` on `relevant()` and `relevant_count()` (it lives in `_relevant_where`, so both gain it at once — which is exactly what the Task 3 signature test enforces).

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
class TestEvidenceFilter:
    async def test_matches_only_decisions_citing_that_evidence(
        self, pg_dsn: str, scratch_schema: str
    ) -> None:
        """Unlike `subject`, evidence has no 'or unscoped' fallback: 'cites
        session X' is an exact question, and folding in decisions that cite
        nothing would be nonsense."""
        bn = await _seeded_client(pg_dsn, scratch_schema)
        cited = await _record_with_evidence(bn, ("session", "sess-42"))
        page = await bn.relevant(evidence=("session", "sess-42"), limit=50)
        assert [d.id for d in page.items] == [cited.decision_id]
        assert await bn.relevant_count(evidence=("session", "sess-42")) == 1
        assert await bn.relevant_count(evidence=("session", "no-such")) == 0
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest -c packages/binnacle-core/pyproject.toml packages/binnacle-core/tests/db/test_query.py -k EvidenceFilter -q`
Expected: FAIL — `TypeError: relevant() got an unexpected keyword argument 'evidence'`

- [ ] **Step 3: Write the migration**

`0004_evidence_ref_index.sql`:

```sql
-- Evidence-ref lookups (REQUIREMENTS FR-6.1's `evidence` filter, added
-- 2026-09-05). idx_refs_subject is partial on `WHERE role = 'subject'`, so it
-- cannot serve a role='evidence' lookup at all -- an evidence filter without
-- this index is a sequential scan of `refs`.

CREATE INDEX idx_refs_evidence ON {schema}.refs(kind, identifier) WHERE role = 'evidence';
```

`0004_evidence_ref_index.rollback.sql`:

```sql
-- Rollback for 0004_evidence_ref_index.sql.

DROP INDEX {schema}.idx_refs_evidence;
```

- [ ] **Step 4: Add the filter to `_relevant_where`**

```python
        if evidence is not None:
            ev_kind, ev_id = evidence
            conditions.append(
                f"EXISTS (SELECT 1 FROM {schema}.refs re "
                "WHERE re.decision_id = d.decision_id AND re.role = 'evidence' "
                "AND re.kind = %(ev_kind)s AND re.identifier = %(ev_id)s)"
            )
            params["ev_kind"] = ev_kind
            params["ev_id"] = ev_id
```

Add `evidence: tuple[str, str] | None` to `_relevant_where`'s signature, to
`PostgresStore.relevant`/`relevant_count`, to the matching `StorePort`
signatures, and to `Binnacle.relevant`/`relevant_count`.

- [ ] **Step 5: Run tests including the migration suite**

Run: `bash scripts/check.sh`
Expected: all gates pass, including `tests/db/test_migrations.py` (which exercises apply and rollback)

- [ ] **Step 6: Commit**

```bash
git add packages/binnacle-core
git commit -m "feat(binnacle-core): add evidence-ref filter and migration 0004

idx_refs_subject is partial on role='subject' and cannot serve evidence
lookups, so the filter ships with its own partial index.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 9: `expiring_before` filter

**Files:**
- Modify: `adapters/postgres_store.py` (`_relevant_where`), `application/ports.py`, `client.py`
- Modify: `packages/binnacle-core/tests/db/test_query.py`

**Interfaces:**
- Produces: `expiring_before: datetime | None = None` on `relevant()` and `relevant_count()`.

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
class TestExpiringBeforeFilter:
    async def test_matches_only_decisions_that_expire_within_the_window(
        self, pg_dsn: str, scratch_schema: str
    ) -> None:
        """The curation journey is 'renew these deliberately before they lapse',
        so a decision with no valid_until must never appear."""
        bn = await _seeded_client(pg_dsn, scratch_schema)
        soon = datetime.now(UTC) + timedelta(days=7)
        expiring = await _record_with_valid_until(bn, soon)
        await _record_short_term(bn, "no expiry at all")

        horizon = datetime.now(UTC) + timedelta(days=14)
        page = await bn.relevant(expiring_before=horizon, limit=50)
        assert [d.id for d in page.items] == [expiring.decision_id]

    async def test_sorting_by_valid_until_excludes_never_expiring_decisions(
        self, pg_dsn: str, scratch_schema: str
    ) -> None:
        """Sorting by an expiry date is meaningless for decisions that have
        none, and NULLs would break the keyset predicate."""
        bn = await _seeded_client(pg_dsn, scratch_schema)
        await _record_short_term(bn, "no expiry at all")
        page = await bn.relevant(sort="valid_until", order="asc", limit=50)
        assert all(d.id is not None for d in page.items)
        assert await bn.relevant_count() > len(page.items)
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest -c packages/binnacle-core/pyproject.toml packages/binnacle-core/tests/db/test_query.py -k ExpiringBefore -q`
Expected: FAIL — `TypeError: relevant() got an unexpected keyword argument 'expiring_before'`

- [ ] **Step 3: Add the filter to `_relevant_where`**

```python
if expiring_before is not None:
    conditions.append("(d.valid_until IS NOT NULL AND d.valid_until < %(expiring_before)s)")
    params["expiring_before"] = expiring_before
```

Thread the parameter through `_relevant_where`, `PostgresStore.relevant` /
`relevant_count`, `StorePort`, and `Binnacle` as in Task 8.

- [ ] **Step 4: Run and commit**

Run: `bash scripts/check.sh`
Expected: all gates pass

```bash
git add packages/binnacle-core
git commit -m "feat(binnacle-core): add expiring_before filter

Answers 'what lapses in the next two weeks' so temporary waivers get
renewed deliberately instead of silently expiring. Excludes decisions
with no valid_until.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 10: `queue_summary()` and `domain_summary()`

**Files:**
- Modify: `adapters/postgres_store.py`, `application/ports.py`, `client.py`
- Modify: `packages/binnacle-core/tests/db/test_aggregates.py`

**Interfaces:**
- Produces: `Binnacle.queue_summary(domains: Sequence[str] | None = None) -> dict[str, int]` and `Binnacle.domain_summary() -> list[DomainSummary]`.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.asyncio
class TestSummaries:
    async def test_queue_summary_counts_open_items_by_kind(
        self, pg_dsn: str, scratch_schema: str
    ) -> None:
        bn = await _seeded_client_with_queue(pg_dsn, scratch_schema)
        summary = await bn.queue_summary()
        open_items = await bn.queue(limit=1000)
        assert sum(summary.values()) == len(open_items.items)

    async def test_queue_summary_ignores_resolved_items(
        self, pg_dsn: str, scratch_schema: str
    ) -> None:
        """A summary that counted resolved items would misreport the review
        backlog as permanently growing."""
        bn = await _seeded_client_with_queue(pg_dsn, scratch_schema)
        before = await bn.queue_summary()
        item = (await bn.queue(limit=1)).items[0]
        await bn.dismiss_item(item.item.item_id, actor=HUMAN, reason="noise")
        after = await bn.queue_summary()
        assert sum(after.values()) == sum(before.values()) - 1

    async def test_domain_summary_includes_domains_with_no_decisions(
        self, pg_dsn: str, scratch_schema: str
    ) -> None:
        """The registry-housekeeping use case is looking for exactly these
        rows, which a plain GROUP BY over decisions would omit."""
        bn = await _seeded_client(pg_dsn, scratch_schema)
        await bn.add_domain("unused", "nothing recorded here yet", actor=HUMAN)
        summaries = {s.name: s for s in await bn.domain_summary()}
        assert summaries["unused"].decision_count == 0
        assert summaries["architecture"].decision_count > 0
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest -c packages/binnacle-core/pyproject.toml packages/binnacle-core/tests/db/test_aggregates.py -k Summaries -q`
Expected: FAIL — `AttributeError: 'Binnacle' object has no attribute 'queue_summary'`

- [ ] **Step 3: Implement both in `PostgresStore`**

```python
async def queue_summary(self, domains: Sequence[str] | None = None) -> dict[str, int]:
    schema = self._schema
    conditions = ["NOT q.resolved"]
    params: dict[str, Any] = {}
    if domains is not None:
        conditions.append("d.domain = ANY(%(domains)s)")
        params["domains"] = list(domains)
    sql = (
        f"SELECT q.kind, COUNT(*) AS n FROM {schema}.queue q "
        f"JOIN {schema}.decisions d ON d.decision_id = q.decision_id "
        f"WHERE {' AND '.join(conditions)} GROUP BY q.kind"
    )
    async with self._read_conn() as conn:
        cur = await conn.execute(sql, params)
        rows = await cur.fetchall()
    return {r["kind"]: int(r["n"]) for r in rows}


async def domain_summary(self) -> list[DomainSummary]:
    schema = self._schema
    sql = (
        f"SELECT dm.name, dm.description, dm.active, "
        f"COUNT(d.decision_id) AS decision_count "
        f"FROM {schema}.domains dm "
        f"LEFT JOIN {schema}.decisions d ON d.domain = dm.name "
        "GROUP BY dm.name, dm.description, dm.active ORDER BY dm.name"
    )
    async with self._read_conn() as conn:
        cur = await conn.execute(sql)
        rows = await cur.fetchall()
    return [
        DomainSummary(
            name=r["name"],
            description=r["description"],
            active=r["active"],
            decision_count=int(r["decision_count"]),
        )
        for r in rows
    ]
```

The `LEFT JOIN` is load-bearing: an inner join or a `GROUP BY` over `decisions`
would silently drop zero-decision domains, which are the rows this method
exists to surface.

- [ ] **Step 4: Add matching `StorePort` methods and thin `Binnacle` delegates**

- [ ] **Step 5: Run and commit**

Run: `bash scripts/check.sh`
Expected: all gates pass

```bash
git add packages/binnacle-core
git commit -m "feat(binnacle-core): add queue_summary() and domain_summary()

Two single-purpose aggregates rather than one dashboard() call. The other
dashboard tiles ('5 stalest', '5 expiring soonest') need no aggregate --
they are relevant() calls with the new sort keys and limit=5.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 11: NFR-7 perf coverage and the export baseline

**Files:**
- Modify: `packages/binnacle-core/tests/db/test_perf.py`

**Interfaces:**
- Consumes: everything from Tasks 3–10.

- [ ] **Step 1: Add timing assertions for the new operations**

Extend `test_nfr7_targets_at_design_scale` with the same measure-and-assert
pattern the existing targets use (follow that test's existing helper for timing
and its generous-CI-bound convention — do not invent a second style):

```python
        await _assert_p95("relevant_count", 0.200, lambda: bn.relevant_count(domains=["perf"]))
        await _assert_p95("queue_summary", 0.100, lambda: bn.queue_summary())
        await _assert_p95("domain_summary", 0.100, lambda: bn.domain_summary())
        await _assert_p95(
            "relevant sort=last_touched_at",
            0.200,
            lambda: bn.relevant(sort="last_touched_at", order="asc", limit=50),
        )
```

If `sort="last_touched_at"` misses its target, that measurement — not a guess —
justifies adding an index; record the number either way.

- [ ] **Step 2: Record the export baseline**

The `binnacle-router` spec defers its "should `/export` stream?" decision to
this number, and only this harness has design-scale data:

```python
        started = time.perf_counter()
        bundle = await bn.export()
        elapsed = time.perf_counter() - started
        size_mb = len(json.dumps(bundle).encode()) / 1_000_000
        print(f"\nNFR-7 export baseline: {size_mb:.1f} MB in {elapsed:.2f}s at design scale")
        assert size_mb < 500, "export bundle far larger than the design scale implies"
```

The assertion is a smoke bound, not the target — the printed number is the
deliverable, to be copied into REQUIREMENTS NFR-7 in Task 12.

- [ ] **Step 3: Run the perf suite**

Run: `uv run pytest -c packages/binnacle-core/pyproject.toml packages/binnacle-core/tests/db/test_perf.py -q -s`
Expected: PASS, with the export baseline printed. Record the printed figures.

- [ ] **Step 4: Commit**

```bash
git add packages/binnacle-core/tests/db/test_perf.py
git commit -m "test(binnacle-core): NFR-7 coverage for the new query surface

Adds targets for relevant_count, both summaries, and the last_touched_at
sort, plus an export size/duration baseline the binnacle-router spec
defers its streaming decision to.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 12: Documentation and version bump

**Files:**
- Modify: `docs/binnacle-core/REQUIREMENTS.md`, `docs/binnacle-core/ARCHITECTURE.md`, `packages/binnacle-core/CHANGELOG.md`, `docs/PROJECT.md`, `packages/binnacle-core/pyproject.toml`

- [ ] **Step 1: Amend REQUIREMENTS.md**

- **FR-6.1** — add sort/order, pagination, `evidence`, and `expiring_before` to the relevance query's description.
- **FR-6.4** — note that queue reads are paginated.
- **FR-6.5** — note the `after_id` tiebreaker.
- **FR-6.10** — new, after FR-6.9: aggregates and counts (`queue_summary()`, `domain_summary()`, `relevant_count()`), stating that `relevant_count()` is a separate call rather than a field on `Page` and why.
- **NFR-7** — add the four rows from Task 11 plus the measured export baseline.

- [ ] **Step 2: Amend ARCHITECTURE.md**

- **§3** — the Query Service component row gains the aggregate/count responsibility.
- **§4** — add `idx_refs_evidence` to the schema block's index list, with the one-line note that `idx_refs_subject`'s partial predicate cannot serve evidence lookups.

- [ ] **Step 3: Update CHANGELOG.md**

```markdown
## [Unreleased]

### Added

- Sortable ordering on `relevant()` (`decided_at`, `recorded_at`,
  `last_touched_at`, `valid_until`), plus `evidence` and `expiring_before`
  filters.
- Keyset pagination on `relevant()` and `queue()` via opaque cursors.
- `relevant_count()`, `queue_summary()`, `domain_summary()`.
- `after_id` tiebreaker on `changes()`.

### Changed

- **Breaking:** `relevant()` and `queue()` return `Page[...]` rather than bare
  lists. An input-only cursor cannot support the derived `last_touched_at`
  sort key, since callers cannot see a derived value in returned rows.
```

- [ ] **Step 4: Update PROJECT.md** with delivery-status entries naming `binnacle-core`.

- [ ] **Step 5: STOP — propose the version bump and wait for explicit confirmation**

GUIDELINES §11 requires the exact bump to be proposed and confirmed before it
is applied, never silently. State to the user: *"`binnacle-core` is at 0.3.0.
These additions are backward-compatible except `relevant()`/`queue()`'s return
type. Pre-1.0, §11 allows a breaking change to ride a minor bump when called
out explicitly — proposing 0.3.0 → 0.4.0 with a `BREAKING CHANGE:` footer.
Confirm, or name a different number."*

Do not proceed to Step 6 until a number is confirmed.

- [ ] **Step 6: Apply the confirmed version** to `packages/binnacle-core/pyproject.toml` and roll `CHANGELOG.md`'s `[Unreleased]` into that version's section, dated.

- [ ] **Step 7: Run the full gate and commit**

Run: `bash scripts/check.sh`
Expected: all gates pass

```bash
git add packages/binnacle-core docs
git commit -m "docs(binnacle-core)!: document the query additions and bump version

BREAKING CHANGE: relevant() and queue() return Page[...] rather than bare
lists. Version confirmed with the user before applying, per GUIDELINES
§11.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Self-Review Notes

- **Spec coverage:** §3.1 → Task 4; §3.2 → Tasks 1, 5, 6; §3.3 → Task 7; §3.4 → Task 8; §3.5 → Task 9; §3.6 → Task 10; §3.7 (count) → Task 3, (drift guard, three layers) → Task 2 (shared builder), Task 3 (signature test), Task 5 (invariant test); §4 public surface → Tasks 1–10; §4.1 doc amendments → Task 12; §5 schema/index → Task 8; §6 performance → Task 11; §8 versioning → Task 12 Step 5.
- **Correction carried from the spec:** the spec proposed `class Page[T](BaseModel)` and flagged pydantic PEP 695 support as a verification item. Reading `domain/models.py` shows every read model is a frozen dataclass, not pydantic — so `Page` is a dataclass and the pydantic question does not arise. The spec's §3.2 verification note is obsolete and should be struck when convenient.
- **Risk carried into every task touching `relevant()`:** its docstring records a prior mypy overload-resolution failure ("Not all union combinations were tried"), and this plan adds five parameters to an already-large `Optional` set. Tasks 4, 5, 8, and 9 each run mypy explicitly for this reason.
- **Placeholder scan:** clean — every step carries runnable code or an exact command.
- **Type consistency:** `Page[T]`, `DomainSummary`, `InvalidCursor`, `encode_cursor`, `decode_cursor`, `_relevant_where`, `_relevant_order`, `_relevant_keyset`, `_SORT_EXPRESSIONS`, `_QUEUE_SORT_EXPRESSIONS`, `relevant_count`, `queue_summary`, `domain_summary` are used consistently across tasks. Task 6 widens `encode_cursor`'s `value` to `datetime | float | None` for `shakiest`'s numeric leading expression — noted there because Task 1 defines it narrower.
