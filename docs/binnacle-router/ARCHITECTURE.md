# ARCHITECTURE — binnacle-router

Architecture for **binnacle-router** (contract: `REQUIREMENTS.md`).
binnacle-router is a Python library — a mountable `fastapi.APIRouter`
translating `binnacle-core`'s async client surface into REST — with no
application, daemon, or configuration store of its own.

## 1. Architectural Position

```
Host process (Meridian, a standalone binnacle deployment, ...)
  │ owns: authentication, FastAPI app, BinnacleConfig, the ActorResolver
  ▼
┌──────────────────────── binnacle-router (library) ────────────────────────┐
│  make_router(binnacle, get_actor) -> APIRouter   install_error_handlers()  │
│  routes/: decisions.py  queue.py  registry.py  feeds.py  sweeps.py         │
│  errors.py: STATUS_BY_ERROR, BinnacleAPIRoute, RFC 7807 problem documents │
│  params.py: paired() query-parameter helper                               │
└──────────────────────────────┬─────────────────────────────────────────────┘
                                │ binnacle_core's public surface only
                                ▼
                    binnacle-core (library): Binnacle client
```

### 1.1 Principles

1. **A router, not an app.** This package's only public entry point besides
   error handling is a factory returning an `APIRouter`; it never
   constructs or ships a `FastAPI` app. A runnable app would recreate
   `binnacle-service`, the daemon `docs/OVERVIEW.md` explicitly declined to
   build — it would conflict with `binnacle-core`'s own FR-8.1 ("library,
   not authority") one layer up, and has no concrete use case: every real
   consumer already has its own app to mount into.
2. **Translate, don't decide.** Every route body is parameter marshaling
   plus exactly one `Binnacle` call (GUIDELINES §8; REQUIREMENTS NFR-1). No
   route re-implements or second-guesses a rule `binnacle-core` already
   enforces — `resolve_conflict`'s request model is the clearest instance
   of this: it accepts `winner_id`, `refined`, and `reason` unconditionally
   and lets core's `LifecycleEngine.resolve_conflict` decide which
   combination is legal, rather than duplicating that judgment as a
   router-level validator that could drift from core's own rule.
3. **The host authenticates; this package attests nothing on its own.**
   Every write-path route depends on a host-supplied `ActorResolver`
   resolved once per request as a FastAPI dependency. This package has no
   login flow, no session, no header it trusts as an identity — see §3.
4. **No new global or static mutable state** (GUIDELINES §8). `make_router`
   and `install_error_handlers` are pure functions of their arguments;
   nothing here is a module-level singleton, so one process can mount
   several independently configured `Binnacle`/`ActorResolver` pairs (a
   staging instance beside production, e.g.) without interference.

## 2. Context (C4 L1)

- **Host process** — constructs `BinnacleConfig` and `Binnacle` (its own
  DSN, schema, `Embedder`), performs its own authentication, supplies an
  `ActorResolver`, and owns the `FastAPI` app this package's router mounts
  into. Meridian is the first concrete host; a standalone binnacle
  deployment is another.
- **binnacle-core** — the only runtime dependency. This package calls it
  exclusively through its public surface (`binnacle_core`'s top-level
  `__init__.py`); see §5 Import Contract.
- **HTTP clients** — anything the host's own app serves to: a UI, another
  service, an agent tool wrapper calling through the host. None of them is
  a direct dependency of this package; they only ever reach it through the
  host's app and the host's authentication.

## 3. The Factory (`make_router`)

```python
def make_router(*, binnacle: Binnacle, get_actor: ActorResolver) -> APIRouter:
    router = APIRouter(prefix="/binnacle/v1", responses=PROBLEM_RESPONSES)
    router.include_router(decision_read_router(binnacle))
    router.include_router(decision_write_router(binnacle, get_actor))
    router.include_router(queue_router(binnacle, get_actor))
    router.include_router(registry_router(binnacle, get_actor))
    router.include_router(feeds_router(binnacle))
    router.include_router(sweeps_router(binnacle))
    return router
```

`binnacle` and `get_actor` are keyword-only and both required: there is no
default `Binnacle` this package could fall back to (`BinnacleConfig`
requires a live, host-fulfilled `Embedder`), and there is no default actor
resolution that would not either reject every write or fabricate an
identity. Read-only sub-routers (`decision_read_router`, `feeds_router`,
`sweeps_router`) take only `binnacle`; write-bearing ones additionally take
`get_actor` — the parameter list itself documents which routes are
attributed, independent of any docstring (REQUIREMENTS FR-6.3's sweep
carve-out is enforced exactly this way: `sweeps_router` has no `get_actor`
parameter to smuggle an actor through at all).

**`ActorResolver`** (`router.py`):

```python
ActorResolver = Callable[..., Awaitable[Actor]]
```

An async callable, because resolving an actor from real authentication is
I/O: a session-store lookup, verifying a JWT signature, or an mTLS peer
read. FastAPI resolves it once per request via
`Annotated[Actor, Depends(get_actor)]` on every write-path route
(`decisions.py`, `queue.py`, `registry.py`) — never on the unattributed
reads (`decision_read_router`, `feeds_router`) or the sweeps
(`sweeps_router`), which do not depend on it at all.

**Actor-attestation boundary.** `docs/binnacle-core/ARCHITECTURE.md` I-2
states plainly that neither an actor's `kind` nor its `id` is independently
verifiable by `binnacle-core` — validating both is the calling party's job,
"including `binnacle-router`, which must resolve kind/id from its own
already-verified authentication, never from a raw client-supplied value."
This package enforces the *shape* of that boundary (no route reads an
`Actor` from a header, query parameter, or body field — REQUIREMENTS
FR-6.1) but cannot enforce a host's resolver actually performing
verification; a host that wires `get_actor` to trust an unverified header
has defeated the boundary despite this package doing everything in its
power. This is why the package README states the constraint explicitly in
the mounting recipe rather than leaving it to be inferred from the type
signature alone.

## 4. Error Mapping (`errors.py`)

`STATUS_BY_ERROR: dict[type[BinnacleError], int]` is the single source of
truth for every core-error-to-status mapping (REQUIREMENTS FR-5.1).
`install_error_handlers(app)` iterates it once, registering the same
handler function for every key via `app.add_exception_handler` — adding a
newly mapped core error is a one-line addition to the dict, not a new
handler function.

**Deliberately no catch-all for `BinnacleError`.** A core error that is
*not* a key in `STATUS_BY_ERROR` propagates unhandled and surfaces as a
500. This is a considered omission, not a gap to fill later: mapping every
`BinnacleError` subclass to some 4xx by default would mean a *new* core
error type — one this package's author has not yet looked at and chosen a
status for — silently becomes "the client's fault" the moment core adds it.
An unmapped 500 is the honest failure mode: it says "something in binnacle
went wrong that this router doesn't know how to classify yet," which is
true, rather than guessing a 4xx that might tell a client it did something
wrong when it did not. `test_an_unmapped_error_is_not_silently_swallowed`
pins this behavior directly.

**Why `install_error_handlers` is separate from `make_router`.** FastAPI
exception handlers attach to the `FastAPI` **app**, not to an `APIRouter` —
there is no router-level hook this package could use to register them as
part of mounting. A host that calls `make_router()` without also calling
`install_error_handlers()` gets a working router whose every typed core
error surfaces as an unmapped, unhelpful 500 instead of the RFC 7807 body
this package exists to produce. This is exactly the kind of implicit
wiring GUIDELINES calls out as painful to rediscover later — hence the
README's mounting recipe includes it as a required, explicitly commented
step, not an optional extra.

**Blast radius decides which of the two mechanisms carries a mapping.**
Starlette dispatches exception handlers by MRO across **every route in the
host's app**, so an app-global handler is only safe for a class a host
route will never raise. That splits the error layer in two:

| Mechanism | Carries | Reaches |
|---|---|---|
| `install_error_handlers(app)` | `STATUS_BY_ERROR`'s `binnacle_core` classes, `RequestValidationError` | the whole app — harmlessly, since no host route raises these |
| `BinnacleAPIRoute` (`route_class`) | `ValueError`/`TypeError` → 422 (FR-5.5) | only routes this package publishes |

`ValueError` and `TypeError` are builtins the host's own code raises
constantly, so the FR-5.2 mapping for them lives in a
`fastapi.routing.APIRoute` subclass whose route handler wraps the call and
converts exactly those two into the same `_problem()` response. Before this
split, mounting binnacle silently changed how the *host's* unrelated
endpoints failed: a host `TypeError`, a `pydantic.ValidationError` or a
`json.JSONDecodeError` (both `ValueError` subclasses) came back as
`422 application/problem+json` with the exception text in `detail` —
leaking host internals, telling the client its request was at fault, and
removing the 5xx the host's alerting watches for. **binnacle does not
intercept the host's own exceptions.**

**`route_class` does not propagate through `include_router`** (verified
against fastapi 0.141, not assumed): an included router's routes are the
route objects that router already built with its own `route_class`, and
`include_router` carries prefix/tags/dependencies/`responses` forward but
not the class. So every one of the six sub-routers passes
`route_class=BinnacleAPIRoute` itself; setting it only on the router
`make_router` returns would protect nothing.
`test_argument_misuse_is_422_on_every_sub_router` exercises one route per
sub-router so a new sub-router that forgets it fails the suite.

**RFC 7807 shape** (`_problem()`): every mapped error, `ValueError`/`TypeError`
misuse, and `RequestValidationError` renders as
`{"type": "https://binnacle.dev/problems/<snake_case>", "title":
"<ClassName>", "status": <int>, "detail": "<message>"}` with media type
`application/problem+json`, `errors` additionally present on validation
failures. One `_problem()` helper is the single place this shape is
constructed, so every error path stays byte-for-byte consistent without
each handler re-deriving it.

**The published 422 declares that shape** (FR-5.6). `make_router` sets
`responses=PROBLEM_RESPONSES` on the router it returns — `responses`
*does* propagate through `include_router`, unlike `route_class` — which
replaces FastAPI's stock `application/json` + `HTTPValidationError`
declaration (an array-valued `detail` this package never sends) with
`application/problem+json` + the `ProblemDocument` model. The schema is
**inlined** rather than referenced as
`#/components/schemas/ProblemDocument`: FastAPI registers a component only
for a `responses` entry naming a `model`, and it then also declares that
model under the route's response-class media type (`application/json`),
which would re-introduce a media type this package never sends. Inlining
costs roughly 20 KB across the ~30 operations and is the only shape that
publishes the real media type alone; revisit if FastAPI gains a way to
register a response component independently of that media type.

## 5. Import Contract

`packages/binnacle-router/pyproject.toml`'s `[tool.importlinter]` section
resolves the dependency-boundary question `docs/OVERVIEW.md` §4 explicitly
left open ("which part of binnacle-core's surface [binnacle-router] may
depend on ... is binnacle-router's own future spec's responsibility to
define and enforce"):

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
allow_indirect_imports = "true"
```

`binnacle_core`'s own `__init__.py` legitimately re-exports from those
three submodules — that re-export **is** its public surface. Without
`allow_indirect_imports = "true"`, import-linter's default indirect-chain
check would flag that internal re-export as if `binnacle_router` itself
reached into `binnacle_core.domain`, which it never does; the flag scopes
the contract to what it is meant to catch: a direct
`from binnacle_core.domain import ...`-style import inside this package's
own source. This contract runs as part of `scripts/check.sh` and CI
(`lint-imports --config packages/binnacle-router/pyproject.toml`) — an
enforced rule, not a documented convention (GUIDELINES §8).

## 6. Streaming Decision for `/export`

`GET /export` returns `Binnacle.export()`'s full JSON-safe bundle as one
response body, not a stream. `docs/binnacle-core/REQUIREMENTS.md` NFR-7
measured this package's deferred question directly: a full, unfiltered
export at design scale (10,000 decisions / 100,000 transitions) produces
28.5 MB of JSON in 0.61s. That is comfortably within an ordinary HTTP
response's practical size and latency budget, so streaming is not adopted
in v1 — filtering (`domains`, `tier`, `status`) already bounds the common
case well below the unfiltered baseline, and revisiting streaming has a
concrete, named trigger (an export response that stops being comfortable
at some future scale) rather than being spent on now.

## 7. Package Layout

```
packages/binnacle-router/src/binnacle_router/
  router.py           make_router(), ActorResolver
  errors.py            STATUS_BY_ERROR, install_error_handlers(),
                       BinnacleAPIRoute, ProblemDocument/PROBLEM_RESPONSES
  params.py            paired() -- shared query-parameter helper
  routes/
    decisions.py        decision_read_router(), decision_write_router()
    queue.py             queue_router()
    registry.py          registry_router() -- domain registry + dashboard summaries
    feeds.py              feeds_router() -- changes / precedent / export
    sweeps.py             sweeps_router() -- the three engine sweeps
```

- Python ≥3.13, async-first, `fastapi>=0.115` — the only third-party
  runtime dependency beyond `binnacle-core` itself.
- No internal layering rules beyond the import-linter contract in §5:
  `routes/*.py` modules are independent of each other (each takes only the
  `binnacle`/`get_actor` it needs) and are composed exclusively by
  `router.py`.
- mypy strict; house gates (`scripts/check.sh`) per GUIDELINES — the same
  gate `binnacle-core` runs, invoked with this package's own
  `pyproject.toml`.
- No architecture-rule tooling beyond import-linter is needed at this
  package's current size (six modules, one factory) — revisit if internal
  structure grows past what a single contract can express.

### 7.1 Decision records

- **DR-1 A router, not an app** (§1.1). Rejected: shipping a runnable
  `FastAPI` app or a `binnacle-service` daemon — no concrete use case,
  and in direct tension with `binnacle-core`'s "library, not authority"
  stance.
- **DR-2 `install_error_handlers` is a separate call from `make_router`**
  (§4), forced by FastAPI's own app-vs-router handler attachment model,
  not a stylistic choice.
- **DR-3 No catch-all `BinnacleError` handler** (§4): an unmapped core
  error is an honest 500, never a guessed 4xx.
- **DR-8 `ValueError`/`TypeError` → 422 is a `route_class`, not an app
  handler** (§4). Rejected: dropping the mapping (FR-5.2 mandates it) and
  keeping it app-global (it changed how the host's own routes fail).
  Rejected: setting `route_class` once on the router `make_router` returns
  — FastAPI does not propagate it through `include_router`.
- **DR-9 The 422's OpenAPI schema is inlined, not a `$ref`** (§4), because
  FastAPI ties component registration for a response to the route's
  response-class media type. Accepted cost: ~20 KB of duplication in the
  published document.
- **DR-4 Response models are `binnacle-core`'s own dataclasses/generics**,
  never a parallel DTO layer (REQUIREMENTS FR-4.1) — verified, not
  assumed, that FastAPI's pydantic-based serialization renders frozen
  dataclasses (including nested generics) faithfully.
- **DR-5 `resolve_conflict`'s request model imposes no shape rule of its
  own** (§1.1 principle 2): core's `InvalidResolution` is the single
  source of truth for which argument combination is legal.
- **DR-6 `/export` is not streamed in v1** (§6), based on a measured
  baseline rather than a guess.
- **DR-7 The sweep endpoints ship with no protection of their own**
  (REQUIREMENTS §5): adding auth/rate-limiting here would either duplicate
  or fight whatever policy the host's own middleware already applies: the
  host is squarely better positioned to decide who may trigger a sweep on
  its own network topology than a library mounted generically into any
  host. This is a documented gap, not an implicit one — the host MUST NOT
  expose these paths publicly.

## 8. Pending Decisions

None outstanding for the REST surface documented here. MCP (REQUIREMENTS
§5) is deferred to its own future architectural decision when that phase
of work begins.
