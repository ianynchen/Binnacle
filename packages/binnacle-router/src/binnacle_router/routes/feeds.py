"""The changes feed, precedent search, and export: direct translations of
`Binnacle`'s three remaining read methods (GUIDELINES §8: no business logic
in transport-layer code). All three are unattributed reads -- unlike
`decisions.py`/`queue.py`/`registry.py`'s writes, none of them takes an
`actor` dependency; `GET /changes`'s `actor_kind`/`actor_id` is a client-
supplied *filter* ("show me changes made by this actor"), completely unlike
the attested actor `get_actor` resolves for a write, and is never passed to
`get_actor`."""

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from binnacle_core import Actor, Binnacle, CompactDecision, PrecedentHit, Tier, Transition
from binnacle_router.errors import BinnacleAPIRoute
from binnacle_router.params import paired


class ChangesQuery(BaseModel):
    """`GET /changes`'s full query parameter set. `actions` needs the
    explicit `Query()` annotation -- a bare `list[str] | None` silently
    arrives as `None` instead of parsing repeated `actions=` params (Task
    4/5 finding, carried forward here). `actor_kind`/`actor_id` pair into
    the `Actor` filter the same way `subject`/`evidence` pair in
    `decisions.py`."""

    since: datetime | None = None
    actions: Annotated[list[str] | None, Query()] = None
    actor_kind: str | None = None
    actor_id: str | None = None
    limit: int = Field(default=500, ge=1)
    after_id: int | None = None


class ChangeEntry(BaseModel):
    """One `changes()` result. `Binnacle.changes()` returns
    `(Transition, CompactDecision)` tuples, which would serialize as a bare
    JSON array of two-element arrays -- a client would have to index by
    position to tell which half is which. Wrapping each pair in named fields
    makes the wire format self-describing."""

    transition: Transition
    decision: CompactDecision


class PrecedentQuery(BaseModel):
    """`GET /precedent`'s full query parameter set. `domains`/`tiers` need
    the explicit `Query()` annotation for the same reason as `actions` above."""

    question: str
    domains: Annotated[list[str] | None, Query()] = None
    tiers: Annotated[list[Tier] | None, Query()] = None
    limit: int = Field(default=10, ge=1)
    include_dead: bool = True


class ExportQuery(BaseModel):
    """`GET /export`'s full query parameter set. `domains`/`status` need the
    explicit `Query()` annotation for the same reason as `actions` above."""

    domains: Annotated[list[str] | None, Query()] = None
    tier: Tier | None = None
    status: Annotated[list[str] | None, Query()] = None


def feeds_router(binnacle: Binnacle) -> APIRouter:
    router = APIRouter(route_class=BinnacleAPIRoute)

    @router.get("/changes")
    async def changes_feed(filters: Annotated[ChangesQuery, Query()]) -> list[ChangeEntry]:
        """The changes feed. Wraps each `(Transition, CompactDecision)` pair
        `Binnacle.changes()` returns as `{"transition": ..., "decision": ...}`
        -- see `ChangeEntry`."""
        actor_pair = paired(
            filters.actor_kind,
            filters.actor_id,
            kind_param="actor_kind",
            identifier_param="actor_id",
        )
        actor = None if actor_pair is None else Actor(*actor_pair)  # type: ignore[arg-type]
        pairs = await binnacle.changes(
            since=filters.since,
            actions=filters.actions,
            actor=actor,
            limit=filters.limit,
            after_id=filters.after_id,
        )
        return [ChangeEntry(transition=t, decision=d) for t, d in pairs]

    @router.get("/precedent")
    async def precedent_search(filters: Annotated[PrecedentQuery, Query()]) -> list[PrecedentHit]:
        return await binnacle.precedent(
            filters.question,
            domains=filters.domains,
            tiers=filters.tiers,
            limit=filters.limit,
            include_dead=filters.include_dead,
        )

    @router.get("/export")
    async def export_snapshot(filters: Annotated[ExportQuery, Query()]) -> dict[str, Any]:
        return await binnacle.export(
            domains=filters.domains,
            tier=filters.tier,
            status=filters.status,
        )

    return router
