"""The mountable router factory.

The host constructs `Binnacle` and passes it in; the router never builds one.
That is forced rather than stylistic: `BinnacleConfig` requires a live
`embedder` object, a host-fulfilled port, and a router that built its own
client would additionally have to read configuration from the environment --
which FR-8.1 forbids one layer down.
"""

from collections.abc import Awaitable, Callable

from fastapi import APIRouter

from binnacle_core import Actor, Binnacle
from binnacle_router.routes.decisions import decision_read_router, decision_write_router
from binnacle_router.routes.queue import queue_router
from binnacle_router.routes.registry import registry_router

ActorResolver = Callable[..., Awaitable[Actor]]
"""How the host supplies the acting identity.

Resolvers are async: a host resolving an actor does so from authentication it
has already performed (a session cookie, a JWT, an mTLS peer), which is I/O.
The router never reads an actor from a client-supplied header -- any caller
could then self-declare `human` and walk through the promotion gate.
"""


def make_router(*, binnacle: Binnacle, get_actor: ActorResolver) -> APIRouter:
    router = APIRouter(prefix="/binnacle/v1")

    router.include_router(decision_read_router(binnacle))
    router.include_router(decision_write_router(binnacle, get_actor))
    router.include_router(queue_router(binnacle, get_actor))
    router.include_router(registry_router(binnacle, get_actor))

    return router
