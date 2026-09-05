# REQUIREMENTS — binnacle-router

Requirements for **binnacle-router**, a library (not a service) exposing
`binnacle-core`'s functionality as a mountable REST surface, so a host
process — a standalone binnacle deployment, or another service such as
Meridian — can embed decision recording, curation, and precedent search
behind its own authentication and app.

Verified against `packages/binnacle-router/src/binnacle_router/` as built
(GUIDELINES §5.3): `router.py`, `errors.py`, `params.py`, and
`routes/{decisions,queue,registry,feeds,sweeps}.py`. MCP is explicitly out
of scope for this document — see §6.

## 1. Problem Statement

`binnacle-core` is an async Python client library: correct, but unusable
from anything that isn't already a Python process linked against it. Every
consumer that needs decision recording, curation, or precedent search over
HTTP — a UI, another service, an agent tool wrapper — would otherwise have
to hand-write its own translation from HTTP requests to `Binnacle` method
calls, its own error-to-status mapping, and its own actor-attestation
wiring, repeatedly and divergently. `binnacle-router` does that translation
once, as a mountable `fastapi.APIRouter`, so a host gets the REST surface by
constructing a `Binnacle` and calling one factory function — never by
running a service binnacle itself ships.

## 2. Glossary

Reuses every term `docs/binnacle-core/REQUIREMENTS.md` §2 defines (Decision,
Tier, Domain, Actor, Transition, Promotion, etc.) unchanged. Router-specific
terms:

- **Host** — the process that constructs `Binnacle` and mounts this
  package's router into its own `FastAPI` app (Meridian, a standalone
  binnacle deployment, or any other embedder). The host owns
  authentication; this package owns none.
- **Actor resolver** (`ActorResolver`) — a host-supplied async callable,
  injected as a FastAPI dependency, that produces an attested `Actor` from
  the host's own already-verified authentication for one request. Every
  write endpoint depends on it; no endpoint ever constructs an `Actor` from
  request data itself.
- **Problem document** — an RFC 7807 (`application/problem+json`) response
  body: `type`, `title`, `status`, `detail`, plus `errors` for
  validation failures. The uniform shape every mapped or validation error
  in this package returns.

## 3. Functional Requirements

### FR-1 Framework and mounting

- **FR-1.1** The package exposes exactly one router factory,
  `make_router(*, binnacle: Binnacle, get_actor: ActorResolver) ->
  APIRouter`. It never constructs a `Binnacle` itself — the host builds one
  (with its own DSN, schema, and `Embedder`) and passes it in, so exactly
  one client per host process configuration exists and this package adds
  no second source of database configuration.
- **FR-1.2** The package ships no `FastAPI` application, no ASGI entry
  point, and no `if __name__ == "__main__"` runner. `binnacle_router` has
  no `app` attribute — a host `include_router()`s the factory's result into
  its own app. This is deliberate, not an oversight: a runnable app here
  would recreate the deferred `binnacle-service` daemon that
  `docs/OVERVIEW.md` explicitly declined to build.
- **FR-1.3** Error mapping is a separate, explicit step:
  `install_error_handlers(app: FastAPI) -> None` registers one exception
  handler per mapped core error onto the host's `FastAPI` app. It is not
  called implicitly by `make_router()` — handlers attach to the app object,
  not to a router, so there is no router-level hook that could install them
  automatically. A host that mounts the router without also calling
  `install_error_handlers()` gets unmapped 500s for every typed core error.

### FR-2 Path convention

- **FR-2.1** Every route is mounted under a fixed `/binnacle/v1` prefix
  (`make_router`'s own `APIRouter(prefix="/binnacle/v1")`) — a request
  outside that prefix is not answered by this router at all (verified by
  `test_router_wiring.py`: `GET /v1/domains` 404s).
- **FR-2.2** Resource collections and items follow plain REST nouns
  (`/decisions`, `/queue`, `/domains`). An action that is neither a create
  nor a full resource replacement — `promote`, `decline`, `discard`,
  `reactivate`, `recommend`, `deactivate`, and the three sweeps — is a verb
  suffixed onto its resource with a colon (`POST
  /queue/{item_id}:promote`, `POST /decisions/{decision_id}:discard`,
  `POST /sweeps:backfill_embeddings`), matching Google's custom-method
  convention. A route with an action suffix directly after a path
  parameter parses the parameter cleanly (`{item_id}` never includes the
  trailing `:promote` text — verified by
  `test_custom_action_suffix_does_not_bleed_into_the_parsed_item_id` and its
  registry-module counterpart).
- **FR-2.3** Bulk lookups that would otherwise require an oversized query
  string use `POST` with a request body instead of `GET` with repeated
  parameters (`POST /decisions:batch_get` for up to arbitrarily many ids —
  200 UUIDs already exceeds common URL length limits).

### FR-3 Endpoint catalog

Every operation below is a direct, unmodified translation of one
`Binnacle` method (GUIDELINES §8: no business logic in transport-layer
code) — this table is the contract; `binnacle-core`'s own REQUIREMENTS.md
FR-1–FR-8 defines what each underlying call actually does. "Actor" means
the endpoint depends on the host's `ActorResolver` (FR-6); its absence
means the read (or, for sweeps, the write) is unattributed.

**Decisions — reads** (`routes/decisions.py`, no actor):

| Method & path | `Binnacle` call | Notes |
|---|---|---|
| `GET /decisions` | `relevant()` | Full filter/sort/pagination set (FR-6.1); `projection=compact\|full` selects `Page[CompactDecision]` or `Page[Decision]` as the response model. |
| `GET /decisions/count` | `relevant_count()` | Shares `_FilterFields` with `GET /decisions` but declares `extra="forbid"`: supplying `sort`/`order`/`after`/`limit`/`projection` is a 422, not a silently ignored parameter (FR-4.2). |
| `GET /decisions/by_source` | `by_source()` | `status`/`tier`/`limit` are omitted from the call entirely when not supplied, not passed as `None` — the store distinguishes the two. |
| `POST /decisions:batch_get` | `get_many()` | Body-carried id list (FR-2.3). |
| `GET /decisions/{decision_id}/history` | `history()` | Returns the full `HistoryRecord` (content, transitions, predecessor/successor/supplement/conflict chains). |

**Decisions — writes** (`routes/decisions.py`, actor required):

| Method & path | `Binnacle` call | Notes |
|---|---|---|
| `POST /decisions` | `record()` | Returns the recorded `Decision`. |
| `POST /decisions/long_term` | `record_long_term()` | Human-only at the core layer (`AuthorityViolation` otherwise); the router does not pre-check the actor kind (FR-4.1). |
| `POST /decisions:promote_refined` | `promote_refined()` | Body carries `source_ids` and a full `refined: NewDecision`. |
| `POST /decisions/{decision_id}/relationships` | `supersede()` or `supplement()` | `kind` is closed to `SUPERSEDES`/`SUPPLEMENTS` in the request model — `PROMOTED_FROM` and `CONFLICTS_WITH` are never client-settable here. |
| `POST /decisions/{decision_id}:recommend` | `recommend()` | Returns `{"item_id": ...}`. |
| `POST /decisions/{decision_id}:discard` | `discard()` | Optional `reason` in the body. |
| `POST /decisions/{decision_id}:reactivate` | `reactivate()` | No body. |

**Queue** (`routes/queue.py`; `GET /queue` unattributed, the five actions actor-required):

| Method & path | `Binnacle` call | Notes |
|---|---|---|
| `GET /queue` | `queue()` | `kinds` needs the explicit `Query()` annotation to parse repeated params (FR-4.3). |
| `POST /queue/{item_id}:promote` | `promote()` | **Returns the promoted `Decision`**, not an empty body — the only queue action with a response payload. |
| `POST /queue/{item_id}:decline` | `decline()` | Optional `reason`. |
| `POST /queue/{item_id}:apply` | `apply_item()` | No body; never legal on a `conflict` item (core raises `InvalidTransition`, mapped to 409). |
| `POST /queue/{item_id}:dismiss` | `dismiss_item()` | Optional `reason`. |
| `POST /queue/{item_id}:resolve_conflict` | `resolve_conflict()` | Request body carries **all three** of `winner_id`, `refined`, and `reason` verbatim; the router does not validate which combination is legal — core does (`InvalidResolution` → 409). See FR-4.4. |

**Domain registry and dashboard summaries** (`routes/registry.py`; reads unattributed, three mutations actor-required):

| Method & path | `Binnacle` call | Notes |
|---|---|---|
| `GET /domains` | `domains()` | Moved here from `router.py`'s placeholder; behavior unchanged. |
| `POST /domains` | `add_domain()` | |
| `PATCH /domains/{name}` | `update_domain()` | Description only — `name` is immutable once registered. |
| `GET /domains/summary` | `domain_summary()` | Includes zero-decision domains (FR-6.10). |
| `POST /domains/{name}:deactivate` | `deactivate_domain()` | Deactivation, never deletion — `DELETE /domains/{name}` is not a route (405). |
| `GET /queue/summary` | `queue_summary()` | Optional `domains` filter. |

**Feeds** (`routes/feeds.py`, no actor — all three are unattributed reads):

| Method & path | `Binnacle` call | Notes |
|---|---|---|
| `GET /changes` | `changes()` | `actor_kind`/`actor_id` here are a client-supplied **filter** ("show me changes made by this actor"), never the attested actor of FR-6 — the two must not be confused. Each `(Transition, CompactDecision)` pair is wrapped as `{"transition": ..., "decision": ...}` (a bare array-of-pairs would not be self-describing). |
| `GET /precedent` | `precedent()` | Embedding-similarity search; `include_dead` defaults to `True` per `binnacle-core`'s own default. |
| `GET /export` | `export()` | Returns the full JSON-safe bundle as-is; not streamed (see ARCHITECTURE §4 for why). |

**Sweeps** (`routes/sweeps.py`, no actor at all — see FR-6.3):

| Method & path | `Binnacle` call | Notes |
|---|---|---|
| `POST /sweeps:backfill_embeddings` | `backfill_embeddings()` | `batch` defaults to 100, matching `Binnacle`'s own default. |
| `POST /sweeps:discover` | `discover()` | Same `batch` default. |
| `POST /sweeps:archive_stale` | `archive_stale()` | No arguments. |

### FR-4 Model reuse and request/response shape

- **FR-4.1** Response models are `binnacle-core`'s own dataclasses and
  generics (`Decision`, `CompactDecision`, `Page[T]`, `HistoryRecord`,
  `QueueItemView`, `DomainRecord`, `DomainSummary`, `PrecedentHit`,
  `ArchivalSummary`, `BackfillSummary`, `DiscoverySummary`) — this package
  defines no parallel DTO layer for what core already models. FastAPI's
  pydantic-based response validation serializes core's frozen dataclasses
  (including nested ones, e.g. `Page[QueueItemView]` wrapping `QueueItem`
  wrapping `Actor`) faithfully, verified field-by-field by
  `test_decisions_read.py` and `test_queue.py` rather than assumed.
- **FR-4.2** Request bodies this package **does** define (`NewDomainRequest`,
  `DomainUpdateRequest`, `ReasonRequest`, `BatchGetRequest`,
  `PromoteRefinedRequest`, `RelationshipRequest`, `ResolveConflictRequest`,
  `BatchRequest`) exist only where core's own method signature takes bare
  positional/keyword arguments rather than one input object — never as a
  restatement of a core model core already defines (`NewDecision` is
  imported and used directly as a request body / nested field, never
  duplicated).
- **FR-4.3** `GET /decisions` and `GET /decisions/count` share their filter
  fields via `_FilterFields`, a common base class, rather than duplicating
  the field list — verified by spike (§ router.py module docstring
  cross-reference) that a route cannot mix one query-parameter model with
  other bare query parameters on the same endpoint, so each endpoint
  declares exactly one model.
- **FR-4.4** List-valued query parameters (`domains`, `status`, `kinds`,
  `actions`, `tiers`) are declared `Annotated[list[T] | None, Query()]`
  everywhere they appear — a bare `list[T] | None` annotation silently
  resolves repeated `key=` parameters to `None` instead of a list (a
  finding from implementation, carried forward as a standing rule across
  every route module).
- **FR-4.5** Paired query parameters that only make sense together
  (`subject_kind`/`subject_identifier`, `evidence_kind`/`evidence_identifier`,
  `actor_kind`/`actor_id`) are validated by the shared `params.paired()`
  helper: supplying exactly one half raises (422 via `BinnacleAPIRoute`,
  FR-5.5) rather than silently widening the query by dropping the
  incomplete filter. The two halves are **not** named to one pattern across
  the surface — `decisions.py` uses `…_identifier`, `/changes` uses
  `actor_id` — so `paired()` takes both parameter names from its caller and
  the 422's `detail` quotes them verbatim. A message naming a parameter the
  endpoint does not declare is unactionable: unknown query parameters are
  ignored, so a client that obeyed it would get the identical 422 forever.

### FR-5 Error mapping (RFC 7807)

- **FR-5.1** `install_error_handlers()` maps every `binnacle-core` error
  type in `STATUS_BY_ERROR` to an HTTP status, one registered handler per
  type:

  | Core error | Status |
  |---|---|
  | `UnknownDomain` | 422 |
  | `InactiveDomain` | 422 |
  | `DecisionNotFound` | 404 |
  | `ItemNotFound` | 404 |
  | `InvalidTransition` | 409 |
  | `InvalidResolution` | 409 |
  | `ItemAlreadyResolved` | 409 |
  | `IdempotencyConflict` | 409 |
  | `AuthorityViolation` | 403 |
  | `InvalidCursor` | 400 |
  | `InvalidSort` | 400 |
  | `EmbeddingDimensionMismatch` | 500 |

  `InvalidCursor`/`InvalidSort` are 400 rather than 422 because a malformed
  cursor or unrecognized sort key is a bad request *parameter*, not a
  semantically invalid body.
- **FR-5.2** Every mapped error, plus a bare `ValueError`/`TypeError` (422 —
  argument misuse the router itself can raise, e.g. `params.paired()`) and
  FastAPI's own `RequestValidationError` (422, field errors carried in an
  `errors` array), renders as an RFC 7807 problem document:
  `{"type": "https://binnacle.dev/problems/<snake_case_error_name>",
  "title": "<ErrorClassName>", "status": <int>, "detail": "<message>"}`,
  media type `application/problem+json`.
- **FR-5.3** There is **no catch-all handler for `BinnacleError`**. A core
  error not present in `STATUS_BY_ERROR` propagates as an unmapped 500,
  deliberately — see ARCHITECTURE §3 for the rationale.
- **FR-5.4** `RequestValidationError`'s `detail` is a field-error summary
  built from `errors()`, never that exception's own `__str__` — this
  FastAPI version's default `__str__` appends the server-side endpoint's
  file path and line number, which a public error body must not leak
  (GUIDELINES §9).
- **FR-5.5** The `ValueError`/`TypeError` → 422 mapping is scoped to this
  package's own routes by a custom `fastapi.routing.APIRoute` subclass
  (`BinnacleAPIRoute`), which every sub-router passes as its `route_class`;
  it is **not** an app-global exception handler. Those two are builtins, and
  Starlette dispatches exception handlers by MRO across every route in the
  host's app — registered app-globally the mapping converted the host's own
  failures (a host `TypeError`, a `pydantic.ValidationError` or a
  `json.JSONDecodeError`, both `ValueError` subclasses) into 422s carrying
  the exception text, leaking host internals and hiding the host's 5xx from
  its own alerting. **Mounting this package must not change how the host's
  own endpoints fail.** Only classes a host route does not raise —
  `binnacle-core`'s own errors and FastAPI's `RequestValidationError` — stay
  in `install_error_handlers`.
- **FR-5.6** The published OpenAPI declares the 422 of every operation as
  `application/problem+json` carrying the `ProblemDocument` schema, the body
  FR-5.2 defines. FastAPI's stock declaration (`application/json` carrying
  `HTTPValidationError`, whose `detail` is an *array*) describes a body this
  package never sends, and a client generated from it breaks on the first
  validation error it sees. Declaring the full per-operation
  400/403/404/409/500 catalog is out of scope (§5).

### FR-6 Actor attestation

- **FR-6.1** Every write endpoint depends on the host-supplied
  `ActorResolver` (`Annotated[Actor, Depends(get_actor)]`) for its acting
  identity. No endpoint constructs an `Actor` from request headers, query
  parameters, or body fields — an actor reaching `binnacle-core` always
  came from the resolver, never from anything client-supplied
  (`test_record_passes_the_resolved_actor_not_a_client_supplied_one` and
  its counterparts in every write-bearing route module assert this
  directly, including against a client that supplies spoofed
  `X-Actor-*` headers the router never reads).
- **FR-6.2** `GET /changes`'s `actor_kind`/`actor_id` query parameters are a
  read-side **filter**, unrelated to FR-6.1's attested actor — they are
  never passed to `get_actor` and never used to construct the identity
  performing any action.
- **FR-6.3** The three sweep endpoints (`routes/sweeps.py`) take **no**
  actor at all — `sweeps_router()` does not accept a `get_actor` parameter,
  so passing a caller-supplied actor through to a sweep is structurally
  impossible, not merely unused. Sweeps self-attribute their own
  transitions to `engine:binnacle` inside `binnacle-core`; `engine` is an
  actor kind that never legitimately crosses this HTTP boundary from a
  human- or agent-facing client.
- **FR-6.4** Per `docs/binnacle-core/ARCHITECTURE.md` I-2, neither an
  actor's `kind` nor its `id` is independently verifiable by
  `binnacle-core` — validating both is entirely the calling party's
  responsibility. For `binnacle-router` specifically, that means the
  `ActorResolver` the host supplies to `make_router()` **must** derive the
  actor from the host's own already-verified authentication (a validated
  session, a decoded and verified JWT, an mTLS peer identity) and never
  from a raw client-supplied value — the router has no mechanism of its
  own to detect a lying resolver.

### FR-7 Import contract

- **FR-7.1** `binnacle_router` depends only on `binnacle_core`'s public
  surface (its top-level `__init__.py` re-exports) — never on
  `binnacle_core.application`, `binnacle_core.domain`, or
  `binnacle_core.adapters` directly. Enforced by an import-linter
  `forbidden` contract in `packages/binnacle-router/pyproject.toml`
  (`[[tool.importlinter.contracts]]`), run as part of `scripts/check.sh`
  and CI — not merely a documented convention (GUIDELINES §8 "architecture
  rules are enforceable, not aspirational").

## 4. Non-Functional Requirements

- **NFR-1 No business logic in transport code.** Every route body is a
  parameter translation plus exactly one `Binnacle` call (GUIDELINES §8);
  no route decides anything a core method doesn't already decide.
- **NFR-2 Async-first, mypy strict, house layering.** Same house standards
  as `binnacle-core` (GUIDELINES.md); enforced by the same `scripts/check.sh`
  gate (ruff, mypy strict, import-linter, pytest) run for this package.
- **NFR-3 No global or process-wide state.** `make_router()` and
  `install_error_handlers()` are pure functions of their arguments; nothing
  in this package holds a module-level `Binnacle` or app instance. Multiple
  hosts, or multiple mounts in one host, may each supply their own
  `Binnacle`/`ActorResolver` pair independently.
- **NFR-4 Honest error surfaces.** An error a client can act on differently
  (missing vs. invalid vs. conflicting vs. forbidden) gets a distinct
  status, never folded into a generic 400/500 (FR-5).

## 5. Out of Scope

- **Bulk queue actions.** Every queue resolution (`promote`, `decline`,
  `apply`, `dismiss`, `resolve_conflict`) operates on exactly one
  `item_id`; there is no batch-resolve endpoint. A host resolving many
  items issues one request per item.
- **Protecting the sweep endpoints.** `POST /sweeps:backfill_embeddings`,
  `POST /sweeps:discover`, and `POST /sweeps:archive_stale` carry no
  authentication, authorization, or rate-limiting of their own — like
  every other route in this package, they trust the host's own middleware
  entirely. Unlike the actor-bearing routes, however, a sweep has no
  `ActorResolver` dependency to even gate on, and no attested-actor audit
  trail names *who* triggered it (sweeps always self-attribute to
  `engine:binnacle`). **A host that mounts this router MUST NOT expose the
  sweep endpoints on a publicly reachable path** — they are meant for a
  host's own internal scheduler or an operator-only network boundary, not
  a client-facing surface.
- **MCP.** `docs/OVERVIEW.md` and this package's own `__init__.py`
  docstring describe an eventual REST **+ MCP** surface; MCP is a
  separately phased scope this document does not cover (out of scope per
  the monorepo restructure design's own phasing) and no MCP code exists
  under `src/` today.
- **`migrate()` is not exposed over HTTP**, deliberately. Schema migration
  is a host-invoked deploy step (`docs/binnacle-core/ARCHITECTURE.md`
  §4.1: "the host decides when to call it"); a remote endpoint capable of
  mutating schema is a security surface with no legitimate client use
  case, so this package defines no route for it. See the package README's
  mounting recipe for the same statement in host-facing terms.
- **The full per-operation response catalog in OpenAPI.** FR-5.6 corrects
  the 422 declaration package-wide; declaring which of 400/403/404/409/500
  each individual operation can return is a large, mechanical change with
  its own review surface and is deferred. Until it lands, the published
  document describes the 422 accurately and is silent about the rest — the
  error-to-status table in FR-5.1 remains the contract for those.
- **Authentication and authorization.** Entirely the host's concern
  (`docs/binnacle-core/REQUIREMENTS.md` §5) — this package has no login
  flow, no session model, and no per-domain ACL of its own; it only
  consumes whatever `Actor` the host's resolver produces.

## 6. Open Questions

One question is open on the REST surface itself: **whether `limit` should
be capped.** It is an unbounded `int` on every paginated endpoint
(`GET /decisions`, `GET /queue`, `GET /changes`, `GET /precedent`, per the
FR-3 catalog) — `binnacle-core` validates neither an oversized nor a
negative value, so both reach it unchanged. See
`docs/binnacle-router/ARCHITECTURE.md` §8: choosing a cap is an API-policy
call for the package owner, so a host mounting this surface publicly
should be aware of it.

MCP scope, per §5, is deferred to its own future planning cycle.
