# binnacle-router REST surface — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `binnacle-router`'s REST surface — a mountable FastAPI `APIRouter` exposing `binnacle-core`'s client API over HTTP, shipping no runnable application.

**Architecture:** A factory, `make_router(binnacle=..., get_actor=...)`, closes over a host-constructed `Binnacle` client and a host-supplied actor resolver and returns an `APIRouter` prefixed `/binnacle/v1`. Every endpoint is a thin translation of one client method; all rules stay in `binnacle-core`. Typed core errors map to HTTP statuses through exception handlers emitting RFC 7807 problem documents.

**Tech Stack:** Python ≥3.13, FastAPI (Starlette/ASGI), `binnacle-core` 0.4.0+, pytest, `unittest.mock.AsyncMock`, mypy strict, import-linter.

**Spec:** [docs/superpowers/specs/2026-09-05-binnacle-router-design.md](../specs/2026-09-05-binnacle-router-design.md) — this plan implements its REST half only (spec §9 phases MCP separately).

## Global Constraints

- **Routes live under `/binnacle/v1/...`** — namespace before version, because the version belongs to *binnacle's* contract, not the host's, and sibling libraries mounted in the same host version independently.
- **`snake_case` everywhere** — paths, custom-action suffixes, JSON body fields, query parameters. Core's models are already snake_case, so no alias layer.
- **The package ships an `APIRouter`, never a runnable app.** A standalone launcher belongs in the README as an example. Shipping one would recreate the deliberately-deferred `binnacle-service` through the back door.
- **`binnacle-router` may import only from `binnacle_core`'s top level** — never `binnacle_core.application.*`, `.domain.*`, or `.adapters.*`. Enforced by import-linter (Task 2).
- **No authentication, no authorization, no business logic.** The host authenticates and attests; `binnacle-core` owns the rules.
- **`migrate()` is exposed on no endpoint** — schema migration is a host-invoked deploy step, and a remote endpoint mutating schema is a security surface with no use case.
- **Externally-supplied actor kinds are `human` and `agent` only.** `engine` never crosses the boundary; sweeps self-attribute internally.
- **Error bodies are RFC 7807** (`application/problem+json`: `type`, `title`, `status`, `detail`).
- ruff line-length 100; mypy strict; `bash scripts/check.sh` is the full gate.

## Corrections to the spec, discovered by reading the code

The spec was written before `binnacle-core` 0.4.0 shipped. Verified against `client.py` and `__init__.py` on 2026-09-05:

1. **Five types the router must name are not publicly exported**: `Tier`, `PrecedentHit`, `BackfillSummary`, `DiscoverySummary`, `ArchivalSummary`. They are return types (or parameter types) of public methods, so a caller can invoke the method but cannot annotate the result without reaching into `binnacle_core.domain.models` — which the import contract forbids. **Task 1 fixes this in `binnacle-core`.**
2. **`source=` cannot fold into `GET /decisions`.** The spec says it is "simply another filter"; `relevant()` has no `source` parameter. `by_source(source, **filters) -> list[CompactDecision]` is a separate, unpaginated method. It gets its own endpoint (Task 4).
3. **`discard()` and `reactivate()` are missing from the spec's catalog** despite DR-3's "REST mirrors the whole client API." Both are real lifecycle operations a UI needs (FR-3.3 discard-as-noise; FR-3.4 revival). Added in Task 5.
4. **`queue_summary()` and `domain_summary()` are missing from the catalog** — they were added to `binnacle-core` after the router spec was written. They are the dashboard's backing endpoints. Added in Task 7.
5. **`recommend()` returns `int | None`** (the new queue item id, or `None`), which the catalog does not mention. Its endpoint returns that id.
6. **`update_domain()` and `deactivate_domain()` exist** as distinct methods, matching the spec's sketched `PATCH` / `:deactivate` shape (Task 7).

Amend the spec to match once this lands.

---

## File Structure

**Create, in `packages/binnacle-router/src/binnacle_router/`:**
- `router.py` — the `make_router()` factory and the `ActorResolver` type. Assembles the sub-routers below. One responsibility: wiring.
- `errors.py` — the core-error → HTTP-status map and the RFC 7807 handlers.
- `routes/decisions.py` — decision reads and writes
- `routes/queue.py` — queue reads and the five resolution actions
- `routes/registry.py` — domains and the two dashboard summaries
- `routes/feeds.py` — changes, precedent, export
- `routes/sweeps.py` — the three engine operations

Split by resource rather than by read/write, so files that change together live together. Each stays small enough to hold in context.

**Create, in `packages/binnacle-router/tests/`:** `conftest.py`, plus one test module per route module mirroring the names above.

**Modify:** `packages/binnacle-router/pyproject.toml` (dependencies, import-linter contract), `packages/binnacle-core/src/binnacle_core/__init__.py` (Task 1), `docs/binnacle-router/{REQUIREMENTS,ARCHITECTURE}.md`, `packages/binnacle-router/{README.md,CHANGELOG.md}`.

---

### Task 1: Export the five missing types from `binnacle-core`

**Files:**
- Modify: `packages/binnacle-core/src/binnacle_core/__init__.py`
- Modify: `packages/binnacle-core/CHANGELOG.md`
- Test: `packages/binnacle-core/tests/unit/test_public_surface.py` (create)

**Interfaces:**
- Produces: `Tier`, `PrecedentHit`, `BackfillSummary`, `DiscoverySummary`, `ArchivalSummary` importable from `binnacle_core`. Every later task depends on this.

- [ ] **Step 1: Write the failing test**

```python
# packages/binnacle-core/tests/unit/test_public_surface.py
"""The public surface must be closed under the signatures it exposes.

A method is only usable if a caller can name what it returns. binnacle-router
imports exclusively from this top-level package (its import-linter contract
forbids reaching into submodules), so a return type that is not re-exported
makes its method effectively unusable from outside."""

import binnacle_core


def test_every_type_named_by_a_public_signature_is_importable() -> None:
    for name in (
        "ArchivalSummary",
        "BackfillSummary",
        "DiscoverySummary",
        "PrecedentHit",
        "Tier",
    ):
        assert hasattr(binnacle_core, name), (
            f"{name} is returned by a public method but not exported"
        )
        assert name in binnacle_core.__all__, f"{name} is importable but missing from __all__"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest -c packages/binnacle-core/pyproject.toml packages/binnacle-core/tests/unit/test_public_surface.py -q`
Expected: FAIL — `ArchivalSummary is returned by a public method but not exported`

- [ ] **Step 3: Add the exports**

In `binnacle_core/__init__.py`, add `ArchivalSummary`, `BackfillSummary`, `DiscoverySummary`, `PrecedentHit`, and `Tier` to the existing `from binnacle_core.domain.models import (...)` block, and add all five to `__all__`. **Both lists are alphabetically sorted — preserve that.**

- [ ] **Step 4: Run the test and the gate**

Run: `uv run pytest -c packages/binnacle-core/pyproject.toml packages/binnacle-core/tests/unit/test_public_surface.py -q`
Expected: PASS

Run: `bash scripts/check.sh`
Expected: all green

- [ ] **Step 5: Add a CHANGELOG entry under `[Unreleased]`**

```markdown
## [Unreleased]

### Added

- `Tier`, `PrecedentHit`, `BackfillSummary`, `DiscoverySummary`, and
  `ArchivalSummary` are re-exported from the top-level package. They are
  named by public method signatures, so callers restricted to the public
  surface previously could not annotate what those methods return.
```

- [ ] **Step 6: Commit**

```bash
git add packages/binnacle-core
git commit -m "feat(binnacle-core): export the types public signatures name

A method is only usable if callers can name its return type. These five
were reachable only via binnacle_core.domain.models, which binnacle-router's
import contract forbids.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

Note: this is an additive public-API change to `binnacle-core`, so it will want a **minor** bump (`0.4.0` → `0.5.0`) at release time. Do not bump here — GUIDELINES §11 requires the bump be proposed and confirmed separately.

---

### Task 2: Package wiring, the router factory, and the test harness

**Files:**
- Modify: `packages/binnacle-router/pyproject.toml`
- Create: `packages/binnacle-router/src/binnacle_router/router.py`
- Modify: `packages/binnacle-router/src/binnacle_router/__init__.py`
- Create: `packages/binnacle-router/tests/conftest.py`
- Create: `packages/binnacle-router/tests/test_router_wiring.py`

**Interfaces:**
- Consumes: Task 1's exports.
- Produces: `make_router(*, binnacle: Binnacle, get_actor: ActorResolver) -> APIRouter`, and `ActorResolver = Callable[..., Awaitable[Actor]]`. Every route task registers into a sub-router that this factory includes.

**Testing approach for this whole plan:** the router is a translation layer, so its tests inject `AsyncMock(spec=Binnacle)` rather than a live database. That lets a test assert *which client method was called with which arguments*, and lets an error-mapping test make the client raise `AuthorityViolation` on demand — both awkward against a real store. Database behavior is already covered by `binnacle-core`'s own suite; re-testing it here would be duplicate coverage in the wrong package.

- [ ] **Step 1: Add dependencies and the import contract to `pyproject.toml`**

```toml
dependencies = [
  "binnacle-core",
  "fastapi>=0.115",
]

[dependency-groups]
dev = ["httpx"]

[tool.uv.sources]
binnacle-core = { workspace = true }
```

and replace the placeholder `[tool.importlinter]` section's comment with the real contract:

```toml
[[tool.importlinter.contracts]]
name = "router depends only on core's public surface"
type = "forbidden"
source_modules = ["binnacle_router"]
forbidden_modules = [
  "binnacle_core.application",
  "binnacle_core.domain",
  "binnacle_core.adapters",
]
```

`httpx` is a dev dependency because FastAPI's `TestClient` needs it; it is not a runtime dependency of the router.

- [ ] **Step 2: Write the failing test**

```python
# packages/binnacle-router/tests/conftest.py
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock

import pytest
from binnacle_core import Actor, Binnacle
from fastapi import FastAPI
from fastapi.testclient import TestClient

from binnacle_router import make_router

HUMAN = Actor("human", "alice")
AGENT = Actor("agent", "meridian/sess-1")


@pytest.fixture()
def client() -> AsyncMock:
    """A stand-in for Binnacle. `spec=` means a typo in a method name fails
    the test rather than silently returning another mock."""
    return AsyncMock(spec=Binnacle)


@pytest.fixture()
def app(client: AsyncMock) -> FastAPI:
    async def get_actor() -> Actor:
        return HUMAN

    application = FastAPI()
    application.include_router(make_router(binnacle=client, get_actor=get_actor))
    return application


@pytest.fixture()
def http(app: FastAPI) -> AsyncIterator[TestClient]:
    with TestClient(app) as c:
        yield c
```

```python
# packages/binnacle-router/tests/test_router_wiring.py
"""The factory's contract: it mounts under /binnacle/v1, it takes the client
and the actor resolver from the host, and it exposes no application of its own."""

from unittest.mock import AsyncMock

from binnacle_core import DomainRecord
from fastapi.testclient import TestClient


def test_routes_are_mounted_under_the_binnacle_v1_namespace(
    http: TestClient, client: AsyncMock
) -> None:
    client.domains.return_value = [DomainRecord(name="architecture", description="d", active=True)]
    assert http.get("/binnacle/v1/domains").status_code == 200
    assert http.get("/v1/domains").status_code == 404, "must not answer outside its namespace"


def test_the_host_supplied_client_is_the_one_called(http: TestClient, client: AsyncMock) -> None:
    """The router must never construct its own Binnacle -- it cannot, since
    BinnacleConfig needs a live host-fulfilled embedder."""
    client.domains.return_value = []
    http.get("/binnacle/v1/domains")
    client.domains.assert_awaited_once()


def test_package_exports_no_runnable_app() -> None:
    """Shipping an app would recreate the deferred binnacle-service."""
    import binnacle_router

    assert not hasattr(binnacle_router, "app")
    assert hasattr(binnacle_router, "make_router")
```

- [ ] **Step 3: Run to verify it fails**

Run: `uv run pytest -c packages/binnacle-router/pyproject.toml packages/binnacle-router/tests -q`
Expected: FAIL — `ImportError: cannot import name 'make_router'`

- [ ] **Step 4: Write `router.py`**

```python
"""The mountable router factory.

The host constructs `Binnacle` and passes it in; the router never builds one.
That is forced rather than stylistic: `BinnacleConfig` requires a live
`embedder` object, a host-fulfilled port, and a router that built its own
client would additionally have to read configuration from the environment --
which FR-8.1 forbids one layer down.
"""

from collections.abc import Awaitable, Callable

from binnacle_core import Actor, Binnacle, DomainRecord
from fastapi import APIRouter, Depends
from typing import Annotated

ActorResolver = Callable[..., Awaitable[Actor]]
"""How the host supplies the acting identity.

Resolvers are async: a host resolving an actor does so from authentication it
has already performed (a session cookie, a JWT, an mTLS peer), which is I/O.
The router never reads an actor from a client-supplied header -- any caller
could then self-declare `human` and walk through the promotion gate.
"""


def make_router(*, binnacle: Binnacle, get_actor: ActorResolver) -> APIRouter:
    router = APIRouter(prefix="/binnacle/v1")
    actor_dep = Depends(get_actor)

    @router.get("/domains")
    async def list_domains() -> list[DomainRecord]:
        return await binnacle.domains()

    return router
```

Export `make_router` and `ActorResolver` from `binnacle_router/__init__.py`, keeping its `__all__` sorted.

- [ ] **Step 5: Run the tests**

Run: `uv run pytest -c packages/binnacle-router/pyproject.toml packages/binnacle-router/tests -q`
Expected: 4 passed (3 new plus the existing package smoke test)

**If FastAPI rejects `list[DomainRecord]` as a response model:** core's read models are frozen *dataclasses*, not pydantic models. Pydantic v2 supports stdlib dataclasses, so this should work — but confirm rather than assume, and if it does not, the fallback is `response_model=None` with explicit serialization. Report which applies; the same question governs every later task's response types. (This is exactly the class of assumption that previously produced a spec claiming `Page` was a `BaseModel`.)

- [ ] **Step 6: Run the full gate and commit**

Run: `bash scripts/check.sh`
Expected: green, including the new import-linter contract

```bash
git add packages/binnacle-router
git commit -m "feat(binnacle-router): add the mountable router factory

make_router() closes over a host-constructed Binnacle and a host-supplied
actor resolver. Ships no app -- that would recreate binnacle-service.
Enables the import contract restricting this package to core's public
surface.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: Error mapping and RFC 7807 problem documents

**Files:**
- Create: `packages/binnacle-router/src/binnacle_router/errors.py`
- Modify: `packages/binnacle-router/src/binnacle_router/router.py`
- Create: `packages/binnacle-router/tests/test_errors.py`

**Interfaces:**
- Produces: `install_error_handlers(app_or_router)` and `STATUS_BY_ERROR: dict[type[BinnacleError], int]`, used by every subsequent endpoint implicitly.

- [ ] **Step 1: Write the failing test**

```python
# packages/binnacle-router/tests/test_errors.py
"""Typed core errors must become real HTTP semantics, not a blanket 500.

The distinction that matters: a missing id *in the URL* is 404, while an
invalid *value inside the request* (an unregistered domain name) is 422 --
they are different failures and a client branches on them differently."""

import pytest
from binnacle_core import (
    AuthorityViolation,
    DecisionNotFound,
    IdempotencyConflict,
    InactiveDomain,
    InvalidCursor,
    InvalidTransition,
    ItemAlreadyResolved,
    ItemNotFound,
    UnknownDomain,
)
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock

CASES = [
    (UnknownDomain("no such domain"), 422),
    (InactiveDomain("deactivated"), 422),
    (DecisionNotFound("missing"), 404),
    (ItemNotFound("missing"), 404),
    (InvalidTransition("illegal"), 409),
    (ItemAlreadyResolved("already"), 409),
    (IdempotencyConflict("diverged"), 409),
    (InvalidCursor("malformed"), 400),
    (AuthorityViolation("agents cannot promote"), 403),
    (ValueError("bad argument"), 422),
]


@pytest.mark.parametrize(("raised", "expected_status"), CASES)
def test_core_errors_map_to_http_status(
    http: TestClient, client: AsyncMock, raised: Exception, expected_status: int
) -> None:
    client.domains.side_effect = raised
    assert http.get("/binnacle/v1/domains").status_code == expected_status


def test_error_body_is_an_rfc7807_problem_document(http: TestClient, client: AsyncMock) -> None:
    client.domains.side_effect = AuthorityViolation("agents cannot promote")
    response = http.get("/binnacle/v1/domains")
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["status"] == 403
    assert body["title"] == "AuthorityViolation"
    assert "agents cannot promote" in body["detail"]
    assert body["type"].endswith("authority_violation")


def test_an_unmapped_error_is_not_silently_swallowed(http: TestClient, client: AsyncMock) -> None:
    """A core error nobody mapped must surface as 500, never as a misleading
    2xx or a wrong 4xx that a client would treat as its own fault."""
    client.domains.side_effect = RuntimeError("something unforeseen")
    with pytest.raises(RuntimeError):
        http.get("/binnacle/v1/domains")
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest -c packages/binnacle-router/pyproject.toml packages/binnacle-router/tests/test_errors.py -q`
Expected: FAIL — every mapped case returns 500

- [ ] **Step 3: Write `errors.py`**

```python
"""Typed core errors as HTTP problem documents (RFC 7807)."""

import re
from typing import Final

from binnacle_core import (
    AuthorityViolation,
    BinnacleError,
    DecisionNotFound,
    EmbeddingDimensionMismatch,
    IdempotencyConflict,
    InactiveDomain,
    InvalidCursor,
    InvalidResolution,
    InvalidSort,
    InvalidTransition,
    ItemAlreadyResolved,
    ItemNotFound,
    UnknownDomain,
)
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

STATUS_BY_ERROR: Final[dict[type[BinnacleError], int]] = {
    UnknownDomain: 422,
    InactiveDomain: 422,
    DecisionNotFound: 404,
    ItemNotFound: 404,
    InvalidTransition: 409,
    InvalidResolution: 409,
    ItemAlreadyResolved: 409,
    IdempotencyConflict: 409,
    AuthorityViolation: 403,
    InvalidCursor: 400,
    InvalidSort: 400,
    EmbeddingDimensionMismatch: 500,
}
"""`InvalidCursor`/`InvalidSort` are 400 rather than 422: a malformed cursor or
an unrecognized sort key is a bad *request parameter*, not a semantically
invalid body."""


def _problem_type(exc: Exception) -> str:
    """A stable, snake_case identifier a client can branch on without parsing prose."""
    return (
        "https://binnacle.dev/problems/"
        + re.sub(r"(?<!^)(?=[A-Z])", "_", type(exc).__name__).lower()
    )


def _problem(exc: Exception, status: int) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        media_type="application/problem+json",
        content={
            "type": _problem_type(exc),
            "title": type(exc).__name__,
            "status": status,
            "detail": str(exc),
        },
    )


def install_error_handlers(app: FastAPI) -> None:
    """Register one handler per mapped core error, plus argument misuse.

    Deliberately no catch-all for `BinnacleError`: an unmapped core error
    should surface as a 500 rather than be guessed into a 4xx that tells the
    client it did something wrong when it did not.
    """

    async def handle_binnacle_error(_: Request, exc: Exception) -> JSONResponse:
        return _problem(exc, STATUS_BY_ERROR[type(exc)])

    for error_type in STATUS_BY_ERROR:
        app.add_exception_handler(error_type, handle_binnacle_error)

    async def handle_argument_misuse(_: Request, exc: Exception) -> JSONResponse:
        return _problem(exc, 422)

    app.add_exception_handler(ValueError, handle_argument_misuse)
    app.add_exception_handler(TypeError, handle_argument_misuse)
```

- [ ] **Step 4: Install the handlers in the test app**

Exception handlers attach to the **application**, not to an `APIRouter` — so `make_router()` cannot register them. Export `install_error_handlers` from `binnacle_router/__init__.py`, call it in `tests/conftest.py`'s `app` fixture, and document in Task 10's README that a host must call it alongside `include_router`.

- [ ] **Step 5: Run the tests, then the gate**

Run: `uv run pytest -c packages/binnacle-router/pyproject.toml packages/binnacle-router/tests -q`
Expected: all pass

Run: `bash scripts/check.sh`

- [ ] **Step 6: Commit**

```bash
git add packages/binnacle-router
git commit -m "feat(binnacle-router): map core errors to RFC 7807 problem documents

No catch-all for BinnacleError: an unmapped error surfaces as 500 rather
than being guessed into a 4xx that blames the client.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: Decision reads

**Files:**
- Create: `packages/binnacle-router/src/binnacle_router/routes/decisions.py`
- Create: `packages/binnacle-router/src/binnacle_router/routes/__init__.py`
- Modify: `packages/binnacle-router/src/binnacle_router/router.py`
- Create: `packages/binnacle-router/tests/test_decisions_read.py`

**Interfaces:**
- Consumes: `make_router` (Task 2), the error handlers (Task 3).
- Produces: `decision_read_router(binnacle: Binnacle) -> APIRouter` — no actor needed; reads are unattributed.

**Endpoints** (each a direct translation, no logic):

| Method + path | Client call |
|---|---|
| `GET /decisions` | `relevant(domains, subject, evidence, status, tier, as_of, expiring_before, text, sort, order, limit, after, include_archived, projection)` → `Page[...]` |
| `GET /decisions/count` | `relevant_count(...)` — same filters, no `sort`/`order`/`after`/`limit`/`projection` |
| `GET /decisions/by_source` | `by_source(source, **filters)` → `list[CompactDecision]`, unpaginated |
| `POST /decisions:batch_get` | `get_many(ids)` — body `{"ids": [...]}` |
| `GET /decisions/{decision_id}/history` | `history(decision_id)` → `HistoryRecord` |

`subject` and `evidence` are `(kind, identifier)` tuples in the client; over HTTP they arrive as two query parameters each — `subject_kind`/`subject_identifier` and `evidence_kind`/`evidence_identifier` — and the endpoint pairs them. Supplying one half without the other is a 422.

- [ ] **Step 1: Write the failing tests**

```python
# packages/binnacle-router/tests/test_decisions_read.py
"""Reads translate query parameters into client arguments and nothing more."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

from binnacle_core import CompactDecision, Page
from fastapi.testclient import TestClient


def _page() -> Page[CompactDecision]:
    return Page(items=[], next_cursor=None)


def test_filters_and_sort_reach_the_client_unchanged(http: TestClient, client: AsyncMock) -> None:
    client.relevant.return_value = _page()
    http.get(
        "/binnacle/v1/decisions",
        params={
            "domains": ["architecture", "testing"],
            "status": ["current"],
            "tier": "long_term",
            "sort": "last_touched_at",
            "order": "asc",
            "limit": 25,
            "subject_kind": "component",
            "subject_identifier": "portolan-ingest",
        },
    )
    kwargs = client.relevant.await_args.kwargs
    assert kwargs["domains"] == ["architecture", "testing"]
    assert kwargs["tier"] == "long_term"
    assert kwargs["sort"] == "last_touched_at"
    assert kwargs["order"] == "asc"
    assert kwargs["limit"] == 25
    assert kwargs["subject"] == ("component", "portolan-ingest")


def test_half_a_subject_pair_is_rejected(http: TestClient, client: AsyncMock) -> None:
    """A subject is a (kind, identifier) pair; half of one is meaningless and
    would otherwise be silently dropped, returning a wider result set than asked for."""
    response = http.get("/binnacle/v1/decisions", params={"subject_kind": "component"})
    assert response.status_code == 422
    client.relevant.assert_not_awaited()


def test_the_cursor_round_trips_verbatim(http: TestClient, client: AsyncMock) -> None:
    client.relevant.return_value = Page(items=[], next_cursor="opaque-token-xyz")
    body = http.get("/binnacle/v1/decisions").json()
    assert body["next_cursor"] == "opaque-token-xyz"

    http.get("/binnacle/v1/decisions", params={"after": "opaque-token-xyz"})
    assert client.relevant.await_args.kwargs["after"] == "opaque-token-xyz"


def test_count_rejects_pagination_parameters(http: TestClient, client: AsyncMock) -> None:
    """sort/after/limit cannot affect a count; accepting them would imply otherwise."""
    client.relevant_count.return_value = 7
    assert http.get("/binnacle/v1/decisions/count").json() == {"count": 7}
    assert (
        http.get("/binnacle/v1/decisions/count", params={"sort": "recorded_at"}).status_code == 422
    )


def test_batch_get_takes_a_body_not_a_query_string(http: TestClient, client: AsyncMock) -> None:
    """200 UUIDs is ~7.4 KB of URL, past common limits."""
    client.get_many.return_value = []
    ids = [str(uuid4()) for _ in range(3)]
    assert http.post("/binnacle/v1/decisions:batch_get", json={"ids": ids}).status_code == 200
    assert [str(i) for i in client.get_many.await_args.args[0]] == ids


def test_history_passes_the_path_id(http: TestClient, client: AsyncMock) -> None:
    decision_id = uuid4()
    client.history.return_value = None
    http.get(f"/binnacle/v1/decisions/{decision_id}/history")
    assert client.history.await_args.args[0] == decision_id


def test_as_of_is_parsed_as_a_datetime(http: TestClient, client: AsyncMock) -> None:
    client.relevant.return_value = _page()
    http.get("/binnacle/v1/decisions", params={"as_of": "2021-03-14T09:22:11Z"})
    assert client.relevant.await_args.kwargs["as_of"] == datetime(
        2021, 3, 14, 9, 22, 11, tzinfo=UTC
    )
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest -c packages/binnacle-router/pyproject.toml packages/binnacle-router/tests/test_decisions_read.py -q`
Expected: FAIL — 404s, since no route exists

- [ ] **Step 3: Implement `routes/decisions.py`**

Write `decision_read_router(binnacle)` returning an `APIRouter` with the five endpoints above. Pair the `subject_kind`/`subject_identifier` and `evidence_kind`/`evidence_identifier` query parameters into tuples, raising `ValueError` (→ 422 via Task 3) when exactly one of a pair is supplied. Declare `sort` and `order` as `Literal` types so FastAPI rejects unknown values with a 422 before they reach the client.

Register the sub-router inside `make_router()` via `router.include_router(decision_read_router(binnacle))`.

- [ ] **Step 4: Run the tests, then the gate**

Run: `uv run pytest -c packages/binnacle-router/pyproject.toml packages/binnacle-router/tests -q`
Expected: all pass

Run: `bash scripts/check.sh`

- [ ] **Step 5: Commit**

```bash
git add packages/binnacle-router
git commit -m "feat(binnacle-router): add decision read endpoints

by_source gets its own endpoint rather than folding into GET /decisions:
relevant() has no source parameter, and by_source returns an unpaginated
list, so folding would have produced an endpoint with two incompatible modes.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: Decision writes

**Files:**
- Modify: `packages/binnacle-router/src/binnacle_router/routes/decisions.py`
- Modify: `packages/binnacle-router/src/binnacle_router/router.py`
- Create: `packages/binnacle-router/tests/test_decisions_write.py`

**Interfaces:**
- Produces: `decision_write_router(binnacle: Binnacle, get_actor: ActorResolver) -> APIRouter`.

**Endpoints:**

| Method + path | Client call | Body |
|---|---|---|
| `POST /decisions` | `record(nd, actor)` | `NewDecision` |
| `POST /decisions/long_term` | `record_long_term(nd, actor)` | `NewDecision` |
| `POST /decisions:promote_refined` | `promote_refined(source_ids, refined, actor)` | `{"source_ids": [...], "refined": {...}}` |
| `POST /decisions/{decision_id}/relationships` | `supersede(new_id, old_id, actor)` or `supplement(...)` | `{"kind": "SUPERSEDES"\|"SUPPLEMENTS", "target_id": "..."}` |
| `POST /decisions/{decision_id}:recommend` | `recommend(decision_id, actor, reason)` → returns the new item id | `{"reason": "..."}` |
| `POST /decisions/{decision_id}:discard` | `discard(decision_id, actor, reason)` | `{"reason": "..."}` |
| `POST /decisions/{decision_id}:reactivate` | `reactivate(decision_id, actor)` | none |

- [ ] **Step 1: Write the failing tests**

```python
# packages/binnacle-router/tests/test_decisions_write.py
"""Writes carry an attested actor and translate one call each."""

from unittest.mock import AsyncMock
from uuid import uuid4

from fastapi.testclient import TestClient

from tests.conftest import HUMAN

NEW_DECISION = {
    "domain": "architecture",
    "scenario": "how should transient failures be handled?",
    "outcome": "retry with exponential backoff, capped at 3 attempts",
    "reasoning": "avoids thundering herd on recovery",
    "source": "meridian",
}


def test_record_passes_the_resolved_actor_not_a_client_supplied_one(
    http: TestClient, client: AsyncMock
) -> None:
    """The actor comes from the host's resolver. If a client-supplied value
    could reach the client, any caller could self-declare `human` and walk
    through the promotion gate."""
    client.record.return_value = None
    http.post(
        "/binnacle/v1/decisions",
        json=NEW_DECISION,
        headers={"X-Actor-Kind": "human", "X-Actor-Id": "mallory"},
    )
    assert client.record.await_args.kwargs["actor"] == HUMAN


def test_relationship_direction_is_path_id_supersedes_target(
    http: TestClient, client: AsyncMock
) -> None:
    """Path id is the `from` side, target_id the `to` side -- matching
    supersede(new_id, old_id) and the links table. Backwards here is data
    corruption, not a cosmetic slip."""
    new_id, old_id = uuid4(), uuid4()
    client.supersede.return_value = None
    http.post(
        f"/binnacle/v1/decisions/{new_id}/relationships",
        json={"kind": "SUPERSEDES", "target_id": str(old_id)},
    )
    assert client.supersede.await_args.args[:2] == (new_id, old_id)


def test_supplements_routes_to_supplement_not_supersede(
    http: TestClient, client: AsyncMock
) -> None:
    new_id, old_id = uuid4(), uuid4()
    client.supplement.return_value = None
    http.post(
        f"/binnacle/v1/decisions/{new_id}/relationships",
        json={"kind": "SUPPLEMENTS", "target_id": str(old_id)},
    )
    client.supplement.assert_awaited_once()
    client.supersede.assert_not_awaited()


def test_an_unknown_relationship_kind_is_rejected(http: TestClient, client: AsyncMock) -> None:
    response = http.post(
        f"/binnacle/v1/decisions/{uuid4()}/relationships",
        json={"kind": "CONFLICTS_WITH", "target_id": str(uuid4())},
    )
    assert response.status_code == 422, "only human-curatable kinds are settable here"


def test_recommend_returns_the_new_queue_item_id(http: TestClient, client: AsyncMock) -> None:
    client.recommend.return_value = 42
    body = http.post(
        f"/binnacle/v1/decisions/{uuid4()}:recommend", json={"reason": "policy"}
    ).json()
    assert body == {"item_id": 42}
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest -c packages/binnacle-router/pyproject.toml packages/binnacle-router/tests/test_decisions_write.py -q`
Expected: FAIL — 404s

- [ ] **Step 3: Implement the write endpoints**

Add `decision_write_router(binnacle, get_actor)` to `routes/decisions.py` and include it in `make_router()`. The relationships endpoint dispatches on `kind`: `SUPERSEDES` → `binnacle.supersede(path_id, target_id, actor=actor)`, `SUPPLEMENTS` → `binnacle.supplement(...)`. Declare `kind` as `Literal["SUPERSEDES", "SUPPLEMENTS"]` so FastAPI rejects anything else with a 422 — `PROMOTED_FROM` is internal provenance and `CONFLICTS_WITH` is set only by `resolve_conflict`, so neither is settable here.

- [ ] **Step 4: Run tests and the gate; commit**

Run: `uv run pytest -c packages/binnacle-router/pyproject.toml packages/binnacle-router/tests -q` then `bash scripts/check.sh`

```bash
git add packages/binnacle-router
git commit -m "feat(binnacle-router): add decision write endpoints

Includes discard and reactivate, absent from the spec's catalog despite
DR-3's 'REST mirrors the whole client API'. Relationship direction is
explicit: path id is the from side, target_id the to side.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 6: Queue

**Files:**
- Create: `packages/binnacle-router/src/binnacle_router/routes/queue.py`
- Modify: `packages/binnacle-router/src/binnacle_router/router.py`
- Create: `packages/binnacle-router/tests/test_queue.py`

**Interfaces:**
- Produces: `queue_router(binnacle: Binnacle, get_actor: ActorResolver) -> APIRouter`.

| Method + path | Client call | Body |
|---|---|---|
| `GET /queue` | `queue(kinds, order, limit, after)` → `Page[QueueItemView]` | — |
| `POST /queue/{item_id}:promote` | `promote(item_id, actor)` | none |
| `POST /queue/{item_id}:decline` | `decline(item_id, actor, reason)` | `{"reason": "..."}` |
| `POST /queue/{item_id}:apply` | `apply_item(item_id, actor)` | none |
| `POST /queue/{item_id}:dismiss` | `dismiss_item(item_id, actor, reason)` | `{"reason": "..."}` |
| `POST /queue/{item_id}:resolve_conflict` | `resolve_conflict(item_id, actor, winner_id=, refined=, reason=)` | exactly one of `winner_id`, `refined`, or `reason` |

- [ ] **Step 1: Write the failing tests**

```python
# packages/binnacle-router/tests/test_queue.py
"""Queue reads paginate; queue actions are human-gated resolutions."""

from unittest.mock import AsyncMock
from uuid import uuid4

from binnacle_core import Page
from fastapi.testclient import TestClient


def test_queue_paginates_like_decisions(http: TestClient, client: AsyncMock) -> None:
    client.queue.return_value = Page(items=[], next_cursor="next-page")
    body = http.get("/binnacle/v1/queue", params={"order": "shakiest", "limit": 10}).json()
    assert body["next_cursor"] == "next-page"
    assert client.queue.await_args.kwargs["order"] == "shakiest"
    assert client.queue.await_args.kwargs["limit"] == 10


def test_resolve_conflict_accepts_exactly_one_resolution(
    http: TestClient, client: AsyncMock
) -> None:
    """The three paths are mutually exclusive by design (FR-5.4). Passing two
    is ambiguous, and guessing which the caller meant could supersede the
    wrong decision."""
    client.resolve_conflict.return_value = None

    assert (
        http.post(
            "/binnacle/v1/queue/1:resolve_conflict", json={"winner_id": str(uuid4())}
        ).status_code
        == 200
    )

    both = http.post(
        "/binnacle/v1/queue/1:resolve_conflict",
        json={"winner_id": str(uuid4()), "reason": "also accepting"},
    )
    assert both.status_code == 422

    neither = http.post("/binnacle/v1/queue/1:resolve_conflict", json={})
    assert neither.status_code == 422


def test_decline_forwards_its_reason(http: TestClient, client: AsyncMock) -> None:
    client.decline.return_value = None
    http.post("/binnacle/v1/queue/7:decline", json={"reason": "style bikeshedding"})
    assert client.decline.await_args.args[0] == 7
    assert client.decline.await_args.kwargs["reason"] == "style bikeshedding"
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest -c packages/binnacle-router/pyproject.toml packages/binnacle-router/tests/test_queue.py -q`
Expected: FAIL — 404s

- [ ] **Step 3: Implement `routes/queue.py`** and include it in `make_router()`. Enforce the resolve-conflict exclusivity in the request model (a pydantic model validator counting the supplied fields), so the 422 comes from validation rather than from a client-side error surfacing later.

- [ ] **Step 4: Run tests and the gate; commit**

```bash
git add packages/binnacle-router
git commit -m "feat(binnacle-router): add queue read and resolution endpoints

resolve_conflict's three paths are mutually exclusive by design, enforced
in validation -- guessing between two supplied resolutions could supersede
the wrong decision.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 7: Domain registry and dashboard summaries

**Files:**
- Create: `packages/binnacle-router/src/binnacle_router/routes/registry.py`
- Modify: `packages/binnacle-router/src/binnacle_router/router.py`
- Create: `packages/binnacle-router/tests/test_registry.py`

**Interfaces:**
- Produces: `registry_router(binnacle: Binnacle, get_actor: ActorResolver) -> APIRouter`. Move the placeholder `GET /domains` from Task 2's `router.py` into this module.

| Method + path | Client call |
|---|---|
| `GET /domains` | `domains()` |
| `POST /domains` | `add_domain(name, description, actor)` |
| `PATCH /domains/{name}` | `update_domain(name, description, actor)` |
| `POST /domains/{name}:deactivate` | `deactivate_domain(name, actor, reason)` |
| `GET /domains/summary` | `domain_summary()` |
| `GET /queue/summary` | `queue_summary(domains)` |

The two summary endpoints back the dashboard and were added to `binnacle-core` after the router spec was written, so they are absent from its catalog.

- [ ] **Step 1: Write the failing tests**

```python
# packages/binnacle-router/tests/test_registry.py
"""Registry management and the two dashboard aggregates."""

from unittest.mock import AsyncMock

from binnacle_core import DomainSummary
from fastapi.testclient import TestClient


def test_domain_summary_reports_zero_decision_domains(http: TestClient, client: AsyncMock) -> None:
    """Zero-decision domains are the rows the registry-housekeeping use case
    exists to surface -- they must survive serialization, not be filtered out."""
    client.domain_summary.return_value = [
        DomainSummary(name="unused", description="nothing here", active=True, decision_count=0)
    ]
    body = http.get("/binnacle/v1/domains/summary").json()
    assert body == [
        {"name": "unused", "description": "nothing here", "active": True, "decision_count": 0}
    ]


def test_queue_summary_forwards_the_domains_filter(http: TestClient, client: AsyncMock) -> None:
    client.queue_summary.return_value = {"promote": 3, "conflict": 1}
    body = http.get("/binnacle/v1/queue/summary", params={"domains": ["architecture"]}).json()
    assert body == {"promote": 3, "conflict": 1}
    assert client.queue_summary.await_args.args[0] == ["architecture"]


def test_deactivating_a_domain_is_a_verb_not_a_delete(http: TestClient, client: AsyncMock) -> None:
    """Nothing in binnacle is deleted; deactivation is a transition-logged act."""
    client.deactivate_domain.return_value = None
    assert (
        http.post(
            "/binnacle/v1/domains/legacy:deactivate", json={"reason": "superseded by architecture"}
        ).status_code
        == 200
    )
    assert http.delete("/binnacle/v1/domains/legacy").status_code == 405
```

- [ ] **Step 2: Run to verify they fail**, implement `routes/registry.py`, move `GET /domains` out of `router.py`, include the sub-router.

Run: `uv run pytest -c packages/binnacle-router/pyproject.toml packages/binnacle-router/tests -q`
Expected: all pass — including Task 2's wiring tests, which still exercise `GET /domains` through its new home

- [ ] **Step 3: Run the gate and commit**

```bash
git add packages/binnacle-router
git commit -m "feat(binnacle-router): add registry and dashboard summary endpoints

queue_summary and domain_summary are absent from the spec's catalog --
they landed in binnacle-core after that spec was written.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 8: Changes, precedent, and export

**Files:**
- Create: `packages/binnacle-router/src/binnacle_router/routes/feeds.py`
- Modify: `packages/binnacle-router/src/binnacle_router/router.py`
- Create: `packages/binnacle-router/tests/test_feeds.py`

**Interfaces:**
- Produces: `feeds_router(binnacle: Binnacle) -> APIRouter`. All three are reads; no actor.

| Method + path | Client call |
|---|---|
| `GET /changes` | `changes(since, actions, actor, limit, after_id)` → `list[tuple[Transition, CompactDecision]]` |
| `GET /precedent` | `precedent(question, domains, tiers, limit, include_dead)` → `list[PrecedentHit]` |
| `GET /export` | `export(domains, tier, status)` → `dict[str, Any]` |

`changes()` returns a list of *tuples*, which serializes as a JSON array of two-element arrays — awkward for a client. Wrap each into an object with named fields (`{"transition": ..., "decision": ...}`) so the wire format is self-describing. State the wrapping in the endpoint's docstring.

`GET /changes` takes an `actor` filter, which is an `Actor` in the client. Accept it as two query parameters, `actor_kind` and `actor_id`, and pair them the same way `subject` is paired in Task 4 — half a pair is a 422.

- [ ] **Step 1: Write the failing tests**

```python
# packages/binnacle-router/tests/test_feeds.py
"""The changes feed, precedent search, and export."""

from unittest.mock import AsyncMock

from fastapi.testclient import TestClient


def test_changes_pairs_are_wrapped_in_named_fields(http: TestClient, client: AsyncMock) -> None:
    """The client returns (Transition, CompactDecision) tuples; a bare JSON
    array of two-element arrays forces clients to index by position."""
    client.changes.return_value = []
    body = http.get("/binnacle/v1/changes").json()
    assert body == []

    assert http.get("/binnacle/v1/changes", params={"actor_kind": "human"}).status_code == 422


def test_export_is_a_single_response(http: TestClient, client: AsyncMock) -> None:
    """Measured at 28.5 MB / 0.61s at design scale, so one response is
    adequate; streaming would need a generator API in core."""
    client.export.return_value = {"schema_version": 1, "decisions": [], "domains": []}
    body = http.get("/binnacle/v1/export", params={"domains": ["architecture"]}).json()
    assert body["schema_version"] == 1
    assert client.export.await_args.kwargs["domains"] == ["architecture"]


def test_precedent_requires_a_question(http: TestClient, client: AsyncMock) -> None:
    client.precedent.return_value = []
    assert http.get("/binnacle/v1/precedent").status_code == 422
    assert (
        http.get("/binnacle/v1/precedent", params={"question": "retry policy?"}).status_code == 200
    )
    assert client.precedent.await_args.args[0] == "retry policy?"
```

- [ ] **Step 2: Run to verify they fail**, implement `routes/feeds.py`, include the sub-router, rerun, run the gate.

- [ ] **Step 3: Commit**

```bash
git add packages/binnacle-router
git commit -m "feat(binnacle-router): add changes, precedent, and export endpoints

changes() returns tuples; wrapped into named fields so clients read by
name rather than position.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 9: Sweeps

**Files:**
- Create: `packages/binnacle-router/src/binnacle_router/routes/sweeps.py`
- Modify: `packages/binnacle-router/src/binnacle_router/router.py`
- Create: `packages/binnacle-router/tests/test_sweeps.py`

**Interfaces:**
- Produces: `sweeps_router(binnacle: Binnacle) -> APIRouter`. **No actor dependency** — the sweeps self-attribute to `engine:binnacle` internally, so they neither need nor accept attestation.

| Method + path | Client call |
|---|---|
| `POST /sweeps:backfill_embeddings` | `backfill_embeddings(batch)` → `BackfillSummary` |
| `POST /sweeps:discover` | `discover(batch)` → `DiscoverySummary` |
| `POST /sweeps:archive_stale` | `archive_stale()` → `ArchivalSummary` |

- [ ] **Step 1: Write the failing tests**

```python
# packages/binnacle-router/tests/test_sweeps.py
"""Engine operations. These are host-scheduled, not user-initiated."""

from unittest.mock import AsyncMock

from fastapi.testclient import TestClient


def test_sweeps_take_no_actor(http: TestClient, client: AsyncMock) -> None:
    """Sweeps attribute their own transitions to engine:binnacle internally.
    Accepting an actor would imply the caller's identity is recorded, which
    would be a lie."""
    client.archive_stale.return_value = None
    http.post("/binnacle/v1/sweeps:archive_stale")
    assert "actor" not in client.archive_stale.await_args.kwargs


def test_batch_size_is_forwarded(http: TestClient, client: AsyncMock) -> None:
    client.discover.return_value = None
    http.post("/binnacle/v1/sweeps:discover", json={"batch": 250})
    assert client.discover.await_args.kwargs["batch"] == 250
```

- [ ] **Step 2: Run to verify they fail**, implement, rerun, run the gate.

- [ ] **Step 3: Commit**

```bash
git add packages/binnacle-router
git commit -m "feat(binnacle-router): add sweep endpoints

No actor dependency: sweeps self-attribute to engine:binnacle, so
accepting one would imply the caller's identity is recorded.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 10: Documentation

**Files:**
- Create: `packages/binnacle-router/README.md`
- Modify: `docs/binnacle-router/REQUIREMENTS.md`, `docs/binnacle-router/ARCHITECTURE.md` (both currently scaffold stubs saying the package has no design yet)
- Modify: `packages/binnacle-router/CHANGELOG.md`, `docs/PROJECT.md`

- [ ] **Step 1: Write the README with a working mounting recipe**

It must carry the complete host-side integration, since findings 2 and 3 of the MCP spike showed that wiring left implicit gets rediscovered painfully:

```python
from binnacle_core import Actor, Binnacle, BinnacleConfig
from binnacle_router import install_error_handlers, make_router
from fastapi import FastAPI, Request

binnacle = Binnacle(BinnacleConfig(dsn=..., embedder=my_embedder))


async def resolve_actor(request: Request) -> Actor:
    session = verify_my_session(request)  # your own auth, already performed
    return Actor(kind="human", id=session.user_id)


app = FastAPI()
install_error_handlers(app)  # required: handlers attach to the app, not the router
app.include_router(make_router(binnacle=binnacle, get_actor=resolve_actor))
```

State plainly that the actor must come from the host's own verified authentication and never from a client-supplied header, and that `migrate()` is deliberately not exposed.

- [ ] **Step 2: Replace the two scaffold docs** with real content — the endpoint catalog as functional requirements in REQUIREMENTS.md, and the factory/error-mapping/import-contract decisions in ARCHITECTURE.md. Both currently say "Status: scaffold… no functional design yet"; remove that framing.

- [ ] **Step 3: Update `packages/binnacle-router/CHANGELOG.md`** with an `[Unreleased]` → `### Added` entry describing the REST surface, and add `docs/PROJECT.md` entries naming the package.

- [ ] **Step 4: Run the gate and commit**

```bash
git add packages/binnacle-router docs
git commit -m "docs(binnacle-router): document the REST surface and mounting recipe

Replaces the scaffold stubs. The README carries the full host-side recipe
including install_error_handlers, which attaches to the app rather than
the router and is therefore easy to omit.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Self-Review Notes

- **Spec coverage:** §4.1 framework → Task 2; §4.1.1 client injection → Task 2; §4.2 path convention → Task 2 (asserted by test); §4.3 catalog → Tasks 4–9; §4.4 model reuse → Task 2 (verified against FastAPI's dataclass support); §4.5 error mapping + RFC 7807 → Task 3; §6 actor attestation → Tasks 2 and 5; §7 import contract → Task 2; §8.1 doc amendments → Task 10; §8.2 known gaps → carried (no bulk queue actions; sweeps unprotected). §5 MCP is **out of scope** — spec §9 phases it separately.
- **Six spec corrections** are recorded above with the code evidence that produced them; the spec should be amended once this lands.
- **Placeholder scan:** clean — every step carries runnable code or an exact command.
- **Type consistency:** `make_router`, `ActorResolver`, `install_error_handlers`, `STATUS_BY_ERROR`, and the five `*_router(...)` factories are used consistently. Task 7 relocates Task 2's placeholder `GET /domains`; Task 2's wiring tests continue to exercise it through the new module rather than being deleted.
- **One risk carried into every task:** FastAPI serializing core's frozen dataclasses as response models. Task 2 Step 5 forces the question early, before nine tasks are built on the assumption — the same mistake shape as the earlier `Page`-is-a-`BaseModel` error.
