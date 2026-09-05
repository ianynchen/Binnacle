# binnacle-router — design spec

Status: proposed (pending user review)
Author: Yining Chen, with Claude Opus 5
Date: 2026-09-05

## 1. Context and goals

`binnacle-router` exposes `binnacle-core`'s functionality over two wire
surfaces — REST and MCP — as a **library, not a service**. A host process
mounts it: a standalone binnacle deployment, or another service such as
meridian. It owns no process, no configuration file, and no authentication.

This preserves `binnacle-core`'s own framing (`FR-8.1`, "library, not
authority") one layer out: `binnacle-router` adds a wire protocol, not a
daemon and not an authority.

Today the package is an empty scaffold created by the monorepo restructure
(`docs/superpowers/specs/2026-09-04-monorepo-restructure-design.md`).

## 2. Non-goals

- **Not a service.** No runnable application ships in this package — see
  DR-1. `binnacle-service` remains deferred.
- **No authentication or authorization.** The host authenticates callers and
  attests actors; `binnacle-router` enforces neither (§6).
- **No schema migration surface.** `migrate()` is deliberately absent from
  both REST and MCP — see DR-2.
- **No business logic.** Every endpoint and tool is a thin translation of one
  `Binnacle` client method. Rules live in `binnacle-core` (GUIDELINES §8:
  "business rules in domain or application services, never in transport-layer
  code").

## 3. Dependency on the `binnacle-core` query additions

The `GET /decisions` and `GET /queue` endpoints depend on
`docs/superpowers/specs/2026-09-05-binnacle-core-query-additions-design.md`
(sortable ordering, keyset pagination). **That spec must land first** — the
browse-and-filter surface both this router and any UI need cannot be built on
`binnacle-core`'s current bare `limit`.

The remaining endpoints have no such dependency and could be built against
`binnacle-core` 0.3.0 as it stands.

## 4. REST surface

### 4.1 Framework and mounting

**FastAPI**, exported as an `APIRouter` the host includes. Rationale over the
alternatives:

- **Async-native (Starlette/ASGI)** — matches `binnacle-core`, which is
  async-first throughout (async psycopg3, async ports). Flask's async support
  is bolted onto a WSGI core, meaning an event-loop bridge on every request.
- **`APIRouter` is purpose-built for the mountable-library case** — the host
  writes `app.include_router(...)`. Django, by contrast, is a
  batteries-included application framework whose shape conflicts with "library,
  not authority," and would need DRF added just to get REST.
- **One runtime for both surfaces** — MCP (§5) can share the same ASGI app and
  event loop rather than running a second concurrency model beside it.

### 4.1.1 How the router obtains its `Binnacle` client

**The host constructs `Binnacle` and passes it in.** The router never builds
one:

```python
def make_router(*, binnacle: Binnacle, get_actor: ActorResolver) -> APIRouter: ...


# host side
app.include_router(make_router(binnacle=my_binnacle, get_actor=meridian_actor))
```

This is forced, not stylistic. `BinnacleConfig` requires a live `embedder`
object, and `Embedder`/`Suggester` are **host-fulfilled ports** (meridian via
tradewind's light tier) — not configuration a router could read from anywhere.
A router that tried to construct its own client could not satisfy them, and
would additionally have to read config from the environment, which FR-8.1
forbids one layer down ("no daemon, no env/file/global reads").

It also avoids a second failure: a host already embedding `binnacle-core` for
its own sweep jobs would otherwise end up with two clients and two connection
pools against one database.

The MCP server object (§5) takes the same two arguments for the same reasons.
This mirrors actor attestation (§6) exactly — the host supplies what it already
owns — so it is one integration pattern, not two.

### 4.2 Path convention

Routes live under **`/binnacle/v1/...`**. The host decides everything above
that (`include_router(..., prefix=...)`); the router bakes in no assumption
about its own mount point beyond its namespace and version.

`binnacle` comes *before* `v1` deliberately. The version belongs to
*binnacle's* API contract, not to the host's — sibling libraries mounted in the
same host (portolan, etc.) version independently, and a bare `/v1/...` would
falsely imply one shared host-wide API version. The namespace segment also
prevents resource-name collisions: `decisions` and `queue` are generic enough
that a host may well have its own.

### 4.3 Endpoint catalog

`snake_case` throughout — paths, custom-action suffixes, JSON body fields, and
query parameters. `binnacle-core`'s models are already snake_case Pydantic, so
this means zero alias/translation layer.

| Method + path | `Binnacle` method |
|---|---|
| `POST /binnacle/v1/decisions` | `record()` — short-term |
| `POST /binnacle/v1/decisions/long_term` | `record_long_term()` — human-only |
| `GET /binnacle/v1/decisions` | `relevant()` — filters, sort, cursor as query params; `source=` folds in `by_source()` (it is simply another filter) |
| `GET /binnacle/v1/decisions/count` | `relevant_count()` — same filter params, no pagination params |
| `POST /binnacle/v1/decisions:batch_get` | `get_many()` — body `{ids: [...]}` |
| `GET /binnacle/v1/decisions/{id}/history` | `history()` |
| `POST /binnacle/v1/decisions/{id}/relationships` | `supersede()` / `supplement()` — body `{kind, target_id}` |
| `POST /binnacle/v1/decisions:promote_refined` | `promote_refined()` — collection-level, takes a list of source ids |
| `GET /binnacle/v1/queue` | `queue()` |
| `POST /binnacle/v1/queue/{item_id}:promote` | `promote()` |
| `POST /binnacle/v1/queue/{item_id}:decline` | `decline()` |
| `POST /binnacle/v1/queue/{item_id}:apply` | `apply_item()` |
| `POST /binnacle/v1/queue/{item_id}:dismiss` | `dismiss_item()` |
| `POST /binnacle/v1/queue/{item_id}:resolve_conflict` | `resolve_conflict()` |
| `GET` / `POST /binnacle/v1/domains` | domain registry read / `add_domain()` |
| `GET /binnacle/v1/changes` | `changes()` |
| `GET /binnacle/v1/precedent` | `precedent()` |
| `GET /binnacle/v1/export` | `export()` |
| `POST /binnacle/v1/sweeps:backfill_embeddings` | `backfill_embeddings()` |
| `POST /binnacle/v1/sweeps:discover` | `discover()` |
| `POST /binnacle/v1/sweeps:archive_stale` | `archive_stale()` |

Relationships are modeled as a **sub-resource** (`/{id}/relationships`) rather
than one custom action per relationship kind, because `links` is a real entity
in the domain model, not an RPC verb. The `:action` suffix is reserved for
things that genuinely are verbs — appropriate here because the domain is
append-only and immutable, so `PUT`/`PATCH` semantics fit almost nothing.

**Relationship direction is explicit.** The path `{id}` is the **`from`** side
and `target_id` the **`to`** side, matching both `supersede(new_id, old_id)` /
`supplement(new_id, old_id)`'s argument order and the `links` table's
`from_id`/`to_id` columns. So `POST /decisions/{new}/relationships` with
`{"kind": "SUPERSEDES", "target_id": "<old>"}` reads as "*new* supersedes
*old*." Stating this is not pedantry: a 50/50 guess at implementation time
produces backwards supersession, which is data corruption rather than a
cosmetic defect.

**`get_many()` is deliberately *not* folded into `GET /decisions`.** Batch
fetch by id and filtered query are incompatible modes: a cursor over a fixed id
list is meaningless, and `ids=` combined with `domains=` has no defined
behavior. It is also a `POST` rather than a `GET` with a query string because
200 UUIDs is roughly 7.4 KB of URL, past common limits — the `:action`
convention already covers POST-for-verb.

### 4.4 Request/response schemas

`binnacle-core`'s own Pydantic models (`NewDecision`, `Ref`, `Decision`,
`QueueItemView`, …) are reused directly as REST bodies. No parallel schema
layer: the two packages version and ship together today, so the only thing a
duplicate set would buy is insulation from internal model changes — not a
concern worth the duplication yet. Revisit if `binnacle-router` ever needs API
stability guarantees independent of `binnacle-core`'s release cadence.

### 4.5 Error mapping

`binnacle-core` raises typed errors; each maps to real HTTP semantics rather
than a blanket 500:

| Error | Status |
|---|---|
| `UnknownDomain`, `InactiveDomain` | 422 Unprocessable Entity |
| `DecisionNotFound`, `ItemNotFound` | 404 Not Found |
| `InvalidTransition`, `InvalidResolution`, `ItemAlreadyResolved`, `IdempotencyConflict` | 409 Conflict |
| `AuthorityViolation` | 403 Forbidden |
| `EmbeddingDimensionMismatch` | 500 Internal Server Error |
| `ValueError` / `TypeError` (argument misuse) | 422 Unprocessable Entity |

Bodies follow **RFC 7807 Problem Details** (`application/problem+json`:
`type`, `title`, `status`, `detail`) — the REST-community standard for this,
rather than a bespoke envelope.

`422` vs `404` is a deliberate distinction: a missing id *in the URL* is 404;
an invalid *value inside the request* (an unregistered domain name) is 422.

## 5. MCP surface

### 5.1 Shaping principle

MCP is **not** a mirror of REST. The tool set is determined by the **authority
model**: an agent gets exactly the operations an agent is permitted to perform.

Every human-gated verb is excluded, and not merely because it would fail — a
tool an agent can see is a tool it will eventually call, and inviting the
attempt undercuts the "suggest, never commit" line the whole system is built on
(NFR-2, I-4).

### 5.2 Tools

| Tool | `Binnacle` method | Why an agent needs it |
|---|---|---|
| `search_precedent` | `precedent()` | "Have we decided something like this before?" — the highest-value tool; what stops re-litigation |
| `get_relevant_decisions` | `relevant()`, compact projection | Top-N context injection at task start |
| `record_decision` | `record()` — always short-term | Write down what the agent settled |
| `recommend_promotion` | `recommend()` | The agent's one legitimate path toward the human gate |
| `get_decision_history` | `history()` | Follow an id from a precedent hit into full reasoning and evolution |
| `list_domains` | domain registry read | Agents must use a registered domain; without this they guess and eat `UnknownDomain` |

Compact projections are the default (FR-6.7 exists because "full reasoning
blobs are a token budget hazard"); full detail is reachable only by asking for
one decision by id.

### 5.3 Deliberate exclusions

- **Every human-gated verb** — `promote`, `promote_refined`, `decline`,
  `record_long_term`, `resolve_conflict`, `apply_item`, `dismiss_item`,
  `add_domain` (§5.1).
- **`export()`** — bulk dump; enormous token cost, no agent use case.
- **The three sweeps** — engine/cron operations, not agent-initiated.
- **`queue()` reads** — the review queue is a human's workspace; an agent
  reading it cannot act on anything in it.
- **`changes()`** — deferred. The "agent rejoining work" journey is real in the
  abstract (README §7.6 names it), but no concrete case for it exists in this
  fleet today, and `search_precedent` already surfaces declined and superseded
  history. Add it when a real resumption workflow appears.
- **`migrate()`** — see DR-2.

### 5.4 Transport

`binnacle-router` provides the MCP **server object** (tool definitions and
handlers); the host chooses transport — mounted over ASGI beside the REST
router, or stdio. This is the same philosophy as shipping an `APIRouter`
instead of an app.

**Requires verification at plan time:** that the chosen MCP SDK's server object
can in fact be ASGI-mounted alongside a FastAPI app on one event loop. This is
asserted here as design intent, not as verified fact — no spike has been run.

## 6. Actor attestation

Both surfaces need an `Actor(kind, id)` for every write. Neither obtains it
from the client.

`binnacle-router` **defines the slot and requires the host to fill it** — for
REST, a FastAPI dependency supplied at mount time; for MCP, an actor resolver
passed at server construction. The host derives the actor from authentication
it already performed (session cookie, JWT, mTLS — its choice).

```python
async def get_actor() -> Actor:
    raise NotImplementedError("host must supply this dependency")


# host side:
app.include_router(binnacle_router, dependencies=[Depends(meridian_get_actor)])
```

The rejected alternative — trusting a client-supplied `X-Actor-Kind` header —
would let any caller self-declare `human` and walk through the promotion gate,
destroying the only structural authority boundary the system has.

**Externally-supplied kinds are `human` and `agent` only.** `engine` never
crosses this boundary: the sweeps attribute their own transitions internally to
a hardcoded `Actor("engine", "binnacle")`.

Neither field is verifiable by `binnacle-core` (documented at
`docs/binnacle-core/ARCHITECTURE.md` I-2); validating both before constructing
the `Actor` is the host's responsibility, and `binnacle-router` inherits that
duty rather than resolving it.

## 7. Package structure and enforcement

`binnacle-router` may import **only from `binnacle_core`'s top-level package** —
never `binnacle_core.application.*`, `.domain.*`, or `.adapters.*`. That
top-level surface is deliberately complete: its own docstring states
"everything else in the package is reachable only through this surface," and
its `__all__` already re-exports every type this router needs (`Decision`,
`CompactDecision`, `Ref`, `Actor`, `Link`, `Transition`, `NewDecision`,
`QueueItem`, `QueueItemView`, `DomainRecord`, `ExportBundle`, `HistoryRecord`,
plus every typed error). No duplicate model set is required, and none should be
created.

Enforced, not aspirational (GUIDELINES §8), in this package's own
`pyproject.toml`:

```toml
[tool.importlinter]
root_package = "binnacle_router"
include_external_packages = true

[[tool.importlinter.contracts]]
name = "router depends only on core's public surface"
type = "forbidden"
source_modules = ["binnacle_router"]
forbidden_modules = ["binnacle_core.application", "binnacle_core.domain", "binnacle_core.adapters"]
```

This resolves DR-7 of the monorepo restructure spec, which deferred the
cross-package dependency rule until this package had a design.

## 8. Decision records

- **DR-1 The package ships an `APIRouter` and an MCP server object — never a
  runnable app.** A standalone launcher lives in the README as an example a
  host copies, not as shipped code. Shipping one would recreate
  `binnacle-service` — deliberately deferred in the monorepo spec (DR-2) — by
  the back door, and would drag process concerns (config, lifecycle, signals)
  into a library.
- **DR-2 `migrate()` is exposed on neither surface.** Schema migration is a
  host-invoked deploy step (`ARCHITECTURE.md` §4.1, "the host decides when to
  call it"); a remote endpoint that mutates schema is a security surface with
  no matching use case.
- **DR-3 REST mirrors the whole client API; MCP mirrors the authority model.**
  The asymmetry is principled, not arbitrary: REST serves hosts and human UIs
  that legitimately need the full surface (including human-gated verbs, since a
  human is behind them); MCP serves agents, who may only do agent-legal things.
  `migrate()` is the single exception on both surfaces (DR-2) — it is an
  operational concern, not part of the client API either surface exists to
  carry.
- **DR-4 `/binnacle/v1/...`, namespace before version.** Version scoping
  belongs to this library, which evolves independently of its host and of
  sibling libraries mounted beside it.
- **DR-5 `binnacle-core`'s models are reused directly as wire schemas.** A
  parallel schema layer buys only independent-versioning insulation, which is
  not needed while the two packages ship together.

### 8.1 Document amendments this triggers

`docs/binnacle-router/REQUIREMENTS.md` and `docs/binnacle-router/ARCHITECTURE.md`
are currently scaffold stubs that explicitly say the package has no functional
design yet. Implementing this spec replaces both with real content — the
endpoint catalog and MCP tool set as functional requirements, and §4.1.1/§6/§7's
integration decisions as architecture. `docs/OVERVIEW.md`'s cross-package
section and `packages/binnacle-router/CHANGELOG.md` follow in the same commit
(GUIDELINES §5).

### 8.2 Known accepted gaps

- **No bulk queue actions.** Declining thirty stale items is thirty round
  trips. Acceptable for a first cut — over an in-process call this was
  negligible, and over HTTP it is merely slow rather than wrong — but it is a
  real UX cost, recorded here rather than discovered later. A
  `POST /queue:batch_dismiss` is the obvious remedy when someone actually feels
  it.
- **Sweep endpoints take no actor.** The three sweeps self-attribute to
  `engine:binnacle` internally, so they neither need nor accept actor
  attestation. Hosts should still protect them: they are operational triggers,
  and nothing in `binnacle-router` stops a caller from hammering `discover()`.

## 9. Implementation phasing

This spec covers two wire surfaces, which is a large single plan. They are
independently shippable and should be phased: **REST first** (it has the
settled design, the larger surface, and the `binnacle-core` dependency to
sequence against), **MCP second** (six tools over an already-working client
integration, plus the §5.4 transport spike). Splitting them into two
implementation plans is expected; splitting the *spec* is not, since the
actor-attestation and package-structure decisions (§6, §7) bind both.

## 10. Open questions

- **Does the REST surface need its own pagination page-size cap?**
  `binnacle-core` will accept a `limit`; a public HTTP surface arguably needs a
  hard maximum that a trusted in-process Python caller does not. Recommended:
  yes, cap it — but the value should be set once the core spec's perf
  measurements exist rather than guessed here.
- **Should `GET /export` stream?** `export()` returns a whole bundle
  synchronously. At NFR-5's stated scale ("thousands, not millions") a single
  response is fine; a streaming variant is a real question only if that bound
  stops holding.
- **MCP SDK ASGI-mounting** — §5.4's verification item, to be settled by a
  spike at plan time before the transport design is locked.
- **Should `record_decision` expose FR-1.6's caller-supplied `decision_id`?**
  Idempotent recording exists so retries don't duplicate, and a transparently
  retried MCP tool call is exactly that scenario — the agent may not even know a
  retry occurred. Exposing the parameter makes retries safe *if* agents supply
  and reuse an id; omitting it means every retry writes a second decision. The
  counter-argument is that asking an LLM to mint and remember a UUID across a
  retry is optimistic, and an unused parameter is just surface area. Worth a
  deliberate call rather than silent omission.
