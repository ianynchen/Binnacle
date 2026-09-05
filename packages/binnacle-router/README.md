# binnacle-router

A mountable `fastapi.APIRouter` exposing `binnacle-core`'s decision
recording, curation, and precedent search as REST. It is a library, not a
service — no daemon, no app of its own, no configuration store. A host
process constructs `Binnacle`, performs its own authentication, and mounts
this package's router into its own `FastAPI` app. See
[`../../docs/binnacle-router/REQUIREMENTS.md`](../../docs/binnacle-router/REQUIREMENTS.md)
and
[`../../docs/binnacle-router/ARCHITECTURE.md`](../../docs/binnacle-router/ARCHITECTURE.md)
for the full contract.

## Contents

[Mounting recipe](#mounting-recipe) · [Actor attestation](#actor-attestation) ·
[Error responses](#error-responses) · [Endpoint catalog](#endpoint-catalog) ·
[Known gaps](#known-gaps) · [Development](#development)

## Mounting recipe

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

Both calls are required and order matters as shown: `install_error_handlers(app)`
registers exception handlers on the `FastAPI` app object itself (FastAPI has
no router-level hook for this), so mounting the router without also calling
it leaves every typed `binnacle-core` error surfacing as an unmapped,
unhelpful 500 instead of the RFC 7807 body this package exists to produce
— see [Error responses](#error-responses).

**Mounting binnacle does not change how your own endpoints fail.** The only
classes `install_error_handlers` registers app-wide are `binnacle-core`'s
own exception types and FastAPI's `RequestValidationError` — nothing your
routes raise. Your `ValueError`s, `TypeError`s, `pydantic.ValidationError`s
and `json.JSONDecodeError`s reach your own handlers and your own 5xx exactly
as they would without binnacle mounted.

**The actor must come from the host's own verified authentication —
never from a client-supplied header, query parameter, or body field.**
`resolve_actor` above is only ever called after `verify_my_session` (or
your equivalent: a validated session, a decoded and verified JWT, an mTLS
peer identity) has already run; `binnacle-core` enforces its authority
rules — "only a human may promote" — against whatever `kind` the resolver
reports, and has no independent way to confirm that report is honest
(`docs/binnacle-core/ARCHITECTURE.md` I-2). A resolver that trusts an
unverified `X-Actor-Kind: human` header would let any caller self-declare
`human` and walk straight through the promotion gate. This package's own
routes never read an actor from request data themselves — every write
endpoint's tests assert this directly, including against a client that
sends spoofed actor headers — but that only closes half the boundary; the
other half is the resolver you write.

**`migrate()` is deliberately not exposed as a route.** Schema migration is
a host-invoked deploy step — `docs/binnacle-core/ARCHITECTURE.md` §4.1:
"the host decides when to call it," typically once at startup or as part
of a deploy pipeline (`await binnacle.migrate()`, `binnacle-core`'s own
method, called directly by the host — never through this router). A remote
HTTP endpoint capable of mutating schema is a security surface with no
legitimate client use case, so none exists here.

## Actor attestation

Every write endpoint depends on the `get_actor` resolver you supply to
`make_router()`, injected once per request as a FastAPI dependency
(`Annotated[Actor, Depends(get_actor)]`). Read-only endpoints
(`GET /decisions`, `GET /queue`, `GET /domains`, `GET /changes`,
`GET /precedent`, `GET /export`) take no actor at all — they are
unattributed, like `binnacle-core`'s own reads. The three sweep endpoints
(`POST /sweeps:*`) also take no actor: they self-attribute to
`engine:binnacle` inside `binnacle-core`, and `sweeps_router` has no
`get_actor` parameter to accept one even by mistake.

`GET /changes`'s `actor_kind`/`actor_id` query parameters are a
**different thing entirely** — a client-supplied read-side filter ("show
me changes made by this actor"), never passed to `get_actor` and never
treated as an attested identity.

## Error responses

Every typed `binnacle-core` error this package knows how to classify comes
back as an [RFC 7807](https://www.rfc-editor.org/rfc/rfc7807) problem
document (`application/problem+json`):

```json
{
  "type": "https://binnacle.dev/problems/decision_not_found",
  "title": "DecisionNotFound",
  "status": 404,
  "detail": "no decision with id 3fa8...5b2c"
}
```

The full error-to-status table lives in
[`../../docs/binnacle-router/REQUIREMENTS.md` FR-5](../../docs/binnacle-router/REQUIREMENTS.md).
FastAPI's own `RequestValidationError` (malformed query parameters, a body
that fails pydantic validation) is mapped the same way, with per-field
detail carried in an `errors` array. `detail` is always a **string**; the
per-field errors live in `errors`, never in `detail`. The published OpenAPI
declares exactly this for every operation's 422 —
`application/problem+json` carrying a `ProblemDocument` schema — rather than
FastAPI's stock `HTTPValidationError`, whose array-valued `detail` this
package never sends. Other error statuses are not yet declared per
operation; FR-5's table is the contract for those.

**`ValueError`/`TypeError` → 422 applies only to binnacle's own routes.**
That mapping is carried by a `route_class` on this package's routers, not by
an app-global exception handler, because Starlette dispatches handlers by
MRO across every route in your app — registered app-wide it would convert
your endpoints' bugs into 422s carrying the exception text.

**There is no catch-all for unmapped `binnacle-core` errors.** A core
error this package's `STATUS_BY_ERROR` does not name propagates and
surfaces as a plain, unmapped 500 — deliberately, so a client is never
told a new, not-yet-classified core error is its own fault. See
`docs/binnacle-router/ARCHITECTURE.md` §4 for the full rationale.

## Endpoint catalog

Every route is mounted under `/binnacle/v1` and is a direct translation of
one `Binnacle` method — no business logic lives in this package. The full
catalog, grouped by resource with the exact `Binnacle` call and any
request/response notes, lives in
[`../../docs/binnacle-router/REQUIREMENTS.md` FR-3](../../docs/binnacle-router/REQUIREMENTS.md);
a short orientation:

| Resource | Routes |
|---|---|
| Decisions (read) | `GET /decisions`, `GET /decisions/count`, `GET /decisions/by_source`, `POST /decisions:batch_get`, `GET /decisions/{id}/history` |
| Decisions (write) | `POST /decisions`, `POST /decisions/long_term`, `POST /decisions:promote_refined`, `POST /decisions/{id}/relationships`, `POST /decisions/{id}:recommend`, `POST /decisions/{id}:discard`, `POST /decisions/{id}:reactivate` |
| Queue | `GET /queue`, `POST /queue/{id}:promote`, `:decline`, `:apply`, `:dismiss`, `:resolve_conflict` |
| Domain registry & dashboards | `GET /domains`, `POST /domains`, `PATCH /domains/{name}`, `GET /domains/summary`, `POST /domains/{name}:deactivate`, `GET /queue/summary` |
| Feeds | `GET /changes`, `GET /precedent`, `GET /export` |
| Sweeps (host-scheduled, no actor) | `POST /sweeps:backfill_embeddings`, `POST /sweeps:discover`, `POST /sweeps:archive_stale` |

A few endpoints worth calling out because their exact shape matters to a
client:

- **`POST /queue/{item_id}:promote` returns the promoted `Decision`**, not
  an empty body — the only queue action with a response payload.
- **`POST /queue/{item_id}:resolve_conflict` accepts `winner_id`,
  `refined`, and `reason` unconditionally** — the router does not
  pre-validate which combination is legal; `binnacle-core` does, raising
  `InvalidResolution` (409) for a shape it rejects. See
  `docs/binnacle-core/REQUIREMENTS.md` FR-5.4 for the resolution rules
  themselves.
- **`GET /decisions/count` rejects pagination parameters (422)** rather
  than silently ignoring them: `sort`, `order`, `after`, `limit`, and
  `projection` cannot affect a count, and accepting them would imply
  otherwise.

## Known gaps

Three gaps are carried deliberately rather than silently omitted:

- **No bulk queue actions.** Every queue resolution operates on exactly
  one `item_id`. Resolving many items is one request per item; there is no
  batch endpoint.
- **The sweep endpoints carry no protection of their own.**
  `POST /sweeps:backfill_embeddings`, `POST /sweeps:discover`, and
  `POST /sweeps:archive_stale` have no authentication, authorization, or
  rate-limiting built into this package — like every route here, they
  trust the host's own middleware entirely, but unlike the actor-bearing
  routes they have no `ActorResolver` dependency to even gate on. **A host
  mounting this router must not expose the sweep endpoints on a publicly
  reachable path** — restrict them to an internal scheduler or an
  operator-only network boundary.
- **`limit` is unbounded on every paginated endpoint.** `GET /decisions`,
  `GET /queue`, `GET /changes`, and `GET /precedent` all accept `limit` as
  a plain `int` with no upper (or lower) bound — `limit=999999999` and
  `limit=-5` both pass straight through to `binnacle-core`, which validates
  neither. Whether to cap it is an API-policy decision for the host to
  make, not one this package has made on the host's behalf; a host that
  cares should validate or clamp `limit` itself before proxying a client's
  value through.

## Development

Uses the same guardrail stack as the rest of the monorepo — run
`bash scripts/check.sh` from the repo root (ruff, mypy strict,
import-linter, and the full test suite for this package, alongside
`binnacle-core` and `binnacle-ui`). See
[`../binnacle-core/README.md` § Development](../binnacle-core/README.md#development)
for the full pre-commit/pre-push/CI breakdown, which applies unchanged
here.

```bash
uv run pytest -c packages/binnacle-router/pyproject.toml packages/binnacle-router/tests
```

Tests run entirely against a `create_autospec(Binnacle, spec_set=True,
instance=True)` — no live database is needed to exercise this package's
own routing, error-mapping, and serialization behavior.
