"""The mountable router factory.

The host constructs `Binnacle` and passes it in; the router never builds one.
That is forced rather than stylistic: `BinnacleConfig` requires a live
`embedder` object, a host-fulfilled port, and a router that built its own
client would additionally have to read configuration from the environment --
which FR-8.1 forbids one layer down.
"""

from collections.abc import Awaitable, Callable

from fastapi import APIRouter

from binnacle_core import Actor, Binnacle, DomainRecord

ActorResolver = Callable[..., Awaitable[Actor]]
"""How the host supplies the acting identity.

Resolvers are async: a host resolving an actor does so from authentication it
has already performed (a session cookie, a JWT, an mTLS peer), which is I/O.
The router never reads an actor from a client-supplied header -- any caller
could then self-declare `human` and walk through the promotion gate.
"""


def make_router(*, binnacle: Binnacle, get_actor: ActorResolver) -> APIRouter:
    router = APIRouter(prefix="/binnacle/v1")

    @router.get("/domains")
    async def list_domains() -> list[DomainRecord]:
        return await binnacle.domains()

    return router
