"""The queue endpoints: the human-gated promotion surface (FR-4). Direct
translations of `Binnacle`'s queue-resolution methods (GUIDELINES §8: no
business logic in transport-layer code) -- `GET /queue` is unattributed, like
`decisions.py`'s reads; the five resolution actions all carry an attested
actor, resolved by the host-supplied `get_actor` and never taken from the
request body or headers (see `router.ActorResolver`'s docstring)."""

from collections.abc import Awaitable, Callable
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from binnacle_core import Actor, Binnacle, Decision, NewDecision, Page, QueueItemView
from binnacle_router.errors import BinnacleAPIRoute

QueueOrder = Literal["oldest", "shakiest", "domain"]


class QueueQuery(BaseModel):
    """`GET /queue`'s full query parameter set. `kinds` needs the explicit
    `Query()` annotation -- a bare `list[str] | None` silently arrives as
    `None` instead of parsing repeated `kinds=` params (Task 4/5 finding)."""

    kinds: Annotated[list[str] | None, Query()] = None
    order: QueueOrder = "oldest"
    limit: int = Field(default=50, ge=1)
    after: str | None = None


class ReasonRequest(BaseModel):
    reason: str | None = None


class ResolveConflictRequest(BaseModel):
    """`resolve_conflict`'s three fields, passed through as given -- which
    combinations are legal is a domain rule that belongs to
    `LifecycleEngine.resolve_conflict` alone (see its docstring), not
    something this transport layer may second-guess. In particular, core
    accepts `winner_id` together with `reason` (the reason becomes the
    discard reason when a long-term winner discards a short-term loser); a
    request-shape validator here that treated `reason` as exclusive with
    `winner_id` would make that legitimate call impossible over REST. Core
    raises `InvalidResolution` for the shapes it does reject, which
    `errors.py` maps to 409.
    """

    winner_id: UUID | None = None
    refined: NewDecision | None = None
    reason: str | None = None


def queue_router(binnacle: Binnacle, get_actor: Callable[..., Awaitable[Actor]]) -> APIRouter:
    router = APIRouter(route_class=BinnacleAPIRoute)

    @router.get("/queue")
    async def list_queue(filters: Annotated[QueueQuery, Query()]) -> Page[QueueItemView]:
        return await binnacle.queue(
            kinds=filters.kinds,
            order=filters.order,
            limit=filters.limit,
            after=filters.after,
        )

    @router.post("/queue/{item_id}:promote")
    async def promote_item(item_id: int, actor: Annotated[Actor, Depends(get_actor)]) -> Decision:
        return await binnacle.promote(item_id, actor=actor)

    @router.post("/queue/{item_id}:decline")
    async def decline_item(
        item_id: int,
        body: ReasonRequest,
        actor: Annotated[Actor, Depends(get_actor)],
    ) -> None:
        await binnacle.decline(item_id, actor=actor, reason=body.reason)

    @router.post("/queue/{item_id}:apply")
    async def apply_item(item_id: int, actor: Annotated[Actor, Depends(get_actor)]) -> None:
        await binnacle.apply_item(item_id, actor=actor)

    @router.post("/queue/{item_id}:dismiss")
    async def dismiss_item(
        item_id: int,
        body: ReasonRequest,
        actor: Annotated[Actor, Depends(get_actor)],
    ) -> None:
        await binnacle.dismiss_item(item_id, actor=actor, reason=body.reason)

    @router.post("/queue/{item_id}:resolve_conflict")
    async def resolve_conflict_item(
        item_id: int,
        body: ResolveConflictRequest,
        actor: Annotated[Actor, Depends(get_actor)],
    ) -> None:
        await binnacle.resolve_conflict(
            item_id,
            actor=actor,
            winner_id=body.winner_id,
            refined=body.refined,
            reason=body.reason,
        )

    return router
