"""binnacle-router: REST + MCP surface for binnacle-core.

Exposes `make_router`, a factory the host mounts into its own FastAPI app --
this package ships no application of its own. See its own future spec/plan
(deferred from docs/superpowers/specs/2026-09-04-monorepo-restructure-design.md
§1) for the full REST/MCP surface design; routes are added incrementally.
"""

from binnacle_router.errors import STATUS_BY_ERROR, install_error_handlers
from binnacle_router.router import ActorResolver, make_router

__all__ = [
    "STATUS_BY_ERROR",
    "ActorResolver",
    "install_error_handlers",
    "make_router",
]

__version__ = "0.1.0"
