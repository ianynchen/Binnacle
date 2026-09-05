"""The domain registry endpoints, plus the two dashboard summary aggregates:
direct translations of `Binnacle`'s registry and summary methods (GUIDELINES
§8: no business logic in transport-layer code). `GET /domains` and the two
summaries are unattributed, like `decisions.py`/`queue.py`'s reads; the three
registry mutations carry an attested actor, resolved by the host-supplied
`get_actor` and never taken from the request body or headers (see
`router.ActorResolver`'s docstring).

`GET /domains` moved here from `router.py`'s Task 2 placeholder -- this is
the only endpoint in the module with no client call of its own to add."""

from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from binnacle_core import Actor, Binnacle, DomainRecord, DomainSummary


class NewDomainRequest(BaseModel):
    name: str
    description: str


class DomainUpdateRequest(BaseModel):
    description: str


class ReasonRequest(BaseModel):
    reason: str | None = None


class QueueSummaryQuery(BaseModel):
    """`GET /queue/summary`'s only query parameter. `domains` needs the
    explicit `Query()` annotation -- a bare `list[str] | None` silently
    arrives as `None` instead of parsing repeated `domains=` params (Task
    4/5 finding, carried forward here)."""

    domains: Annotated[list[str] | None, Query()] = None


def registry_router(binnacle: Binnacle, get_actor: Callable[..., Awaitable[Actor]]) -> APIRouter:
    router = APIRouter()

    @router.get("/domains")
    async def list_domains() -> list[DomainRecord]:
        return await binnacle.domains()

    @router.post("/domains")
    async def add_domain(
        body: NewDomainRequest, actor: Annotated[Actor, Depends(get_actor)]
    ) -> None:
        await binnacle.add_domain(body.name, body.description, actor=actor)

    @router.patch("/domains/{name}")
    async def update_domain(
        name: str,
        body: DomainUpdateRequest,
        actor: Annotated[Actor, Depends(get_actor)],
    ) -> None:
        await binnacle.update_domain(name, body.description, actor=actor)

    @router.get("/domains/summary")
    async def domain_summary() -> list[DomainSummary]:
        return await binnacle.domain_summary()

    @router.post("/domains/{name}:deactivate")
    async def deactivate_domain(
        name: str,
        body: ReasonRequest,
        actor: Annotated[Actor, Depends(get_actor)],
    ) -> None:
        await binnacle.deactivate_domain(name, actor=actor, reason=body.reason)

    @router.get("/queue/summary")
    async def queue_summary(filters: Annotated[QueueSummaryQuery, Query()]) -> dict[str, int]:
        return await binnacle.queue_summary(filters.domains)

    return router
