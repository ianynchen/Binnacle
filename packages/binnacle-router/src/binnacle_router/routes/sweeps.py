"""The three engine sweeps: host-scheduled maintenance operations, not
user-initiated ones. Direct translations of `Binnacle`'s sweep methods
(GUIDELINES §8: no business logic in transport-layer code). Unlike every
other write endpoint in this package, none of these takes an actor
dependency: each sweep self-attributes its own transitions to
`engine:binnacle` internally, and `engine` is an actor kind that never
crosses this HTTP boundary (see `router.ActorResolver`'s docstring) --
accepting a caller-supplied actor here would misrepresent whose identity
gets recorded. `sweeps_router` therefore takes only `binnacle`, like
`feeds_router`."""

from fastapi import APIRouter
from pydantic import BaseModel

from binnacle_core import ArchivalSummary, BackfillSummary, Binnacle, DiscoverySummary
from binnacle_router.errors import BinnacleAPIRoute


class BatchRequest(BaseModel):
    """`backfill_embeddings`/`discover`'s shared `batch` argument. Defaults
    to 100, matching `Binnacle`'s own default, so an empty body behaves
    identically to calling the client method directly."""

    batch: int = 100


def sweeps_router(binnacle: Binnacle) -> APIRouter:
    router = APIRouter(route_class=BinnacleAPIRoute)

    @router.post("/sweeps:backfill_embeddings")
    async def backfill_embeddings_sweep(body: BatchRequest) -> BackfillSummary:
        return await binnacle.backfill_embeddings(batch=body.batch)

    @router.post("/sweeps:discover")
    async def discover_sweep(body: BatchRequest) -> DiscoverySummary:
        return await binnacle.discover(batch=body.batch)

    @router.post("/sweeps:archive_stale")
    async def archive_stale_sweep() -> ArchivalSummary:
        return await binnacle.archive_stale()

    return router
