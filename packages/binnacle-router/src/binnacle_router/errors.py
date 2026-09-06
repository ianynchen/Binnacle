"""Typed core errors as HTTP problem documents (RFC 7807).

Two mechanisms, split by blast radius. `install_error_handlers(app)`
registers *app-global* handlers, so it carries only `binnacle_core`'s own
exception classes plus FastAPI's `RequestValidationError` -- classes a host
route cannot raise by accident. `BinnacleAPIRoute` carries the
`ValueError`/`TypeError` -> 422 mapping instead, because those two are
builtins the host's own routes raise all the time; as an app-global handler
the mapping reached every route in the host's app (Starlette dispatches
handlers by MRO app-wide), turning the host's own bugs into 422s.
"""

import re
from collections.abc import Callable, Coroutine
from typing import Any, Final, cast

from fastapi import FastAPI, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel

from binnacle_core import (
    AuthorityViolation,
    BinnacleError,
    DecisionNotFound,
    EmbeddingDimensionMismatch,
    IdempotencyConflict,
    InactiveDomain,
    InvalidCursor,
    InvalidResolution,
    InvalidSort,
    InvalidTransition,
    ItemAlreadyResolved,
    ItemNotFound,
    UnknownDomain,
)

STATUS_BY_ERROR: Final[dict[type[BinnacleError], int]] = {
    UnknownDomain: 422,
    InactiveDomain: 422,
    DecisionNotFound: 404,
    ItemNotFound: 404,
    InvalidTransition: 409,
    InvalidResolution: 409,
    ItemAlreadyResolved: 409,
    IdempotencyConflict: 409,
    AuthorityViolation: 403,
    InvalidCursor: 400,
    InvalidSort: 400,
    EmbeddingDimensionMismatch: 500,
}
"""`InvalidCursor`/`InvalidSort` are 400 rather than 422: a malformed cursor or
an unrecognized sort key is a bad *request parameter*, not a semantically
invalid body."""


class ProblemDocument(BaseModel):
    """An RFC 7807 error body, with `errors` carrying per-field validation
    detail when the failure was a request-validation one."""

    # This model is declared, never serialized: `_problem()` below builds every
    # error response's `JSONResponse` directly, and this exists so the
    # published OpenAPI can describe that body to a client generating code from
    # the schema. The docstring above is deliberately short because pydantic
    # publishes it as the schema's `description`, once per operation.
    # `test_openapi.py` validates real 422 bodies against this model, so the
    # declaration cannot drift from what the handlers actually send.

    type: str
    title: str
    status: int
    detail: str
    errors: list[dict[str, Any]] | None = None


PROBLEM_RESPONSES: Final[dict[int | str, dict[str, Any]]] = {
    422: {
        "description": "Argument misuse or a request that failed validation, as an RFC 7807 "
        "problem document.",
        "content": {"application/problem+json": {"schema": ProblemDocument.model_json_schema()}},
    }
}
"""The 422 declaration `make_router` puts on every route it publishes.

The schema is inlined rather than referenced as
`#/components/schemas/ProblemDocument`: FastAPI only registers a component for
a `responses` entry that names a `model`, and it then also declares that model
under `application/json` -- a media type this package never sends. An inlined
schema is the only shape that publishes the real media type alone. Revisit if
FastAPI gains a way to register a response component without binding it to the
route's response-class media type.

Only 422 is declared. The full per-operation 400/403/404/409/500 catalog is a
separate, larger change and is deliberately not started here.
"""


def _problem_type(exc: Exception) -> str:
    """A stable, snake_case identifier a client can branch on without parsing prose."""
    return (
        "https://binnacle.dev/problems/"
        + re.sub(r"(?<!^)(?=[A-Z])", "_", type(exc).__name__).lower()
    )


def _problem(
    exc: Exception,
    status: int,
    *,
    detail: str | None = None,
    extra: dict[str, object] | None = None,
) -> JSONResponse:
    content: dict[str, object] = {
        "type": _problem_type(exc),
        "title": type(exc).__name__,
        "status": status,
        "detail": str(exc) if detail is None else detail,
    }
    if extra:
        content.update(extra)
    return JSONResponse(
        status_code=status,
        media_type="application/problem+json",
        content=content,
    )


class BinnacleAPIRoute(APIRoute):
    """The route class every sub-router in this package uses, converting a
    `ValueError`/`TypeError` raised inside one of *binnacle's own* handlers
    into the 422 problem document FR-5.2 mandates.

    Scoped here rather than registered on the app: `ValueError` and
    `TypeError` are builtins, and Starlette dispatches exception handlers by
    MRO across every route in the host's app. As an app-global handler the
    mapping converted the host's own failures too -- a host `TypeError`, a
    `pydantic.ValidationError` or a `json.JSONDecodeError` (both `ValueError`
    subclasses) came back as 422 with the exception text in the body, leaking
    host internals and hiding the host's 5xx from its own alerting. A route
    class reaches exactly the routes this package publishes.

    Typed `binnacle_core` errors are unaffected and keep their app-global
    handlers: `BinnacleError` derives from `Exception`, not `ValueError`, so
    none of them is caught here -- if that ever changed, a core error would
    silently become 422 instead of its mapped status, which
    `test_core_errors_map_to_http_status` would catch.

    Each sub-router must pass this class itself: FastAPI does **not**
    propagate `route_class` through `include_router` (verified against
    fastapi 0.141 -- an included router's routes are the objects that router
    built with its own route class), so setting it only on the router
    `make_router` returns would leave every route unprotected.
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def convert_argument_misuse(request: Request) -> Response:
            try:
                return await handler(request)
            except (ValueError, TypeError) as exc:
                return _problem(exc, 422)

        return convert_argument_misuse


def install_error_handlers(app: FastAPI) -> None:
    """Register one handler per mapped core error, plus FastAPI's own
    request-validation errors.

    Required alongside `make_router`: these handlers attach to the `FastAPI`
    app, which an `APIRouter` has no hook to do for itself. Every class
    registered here is either a `binnacle_core` error or FastAPI's own
    `RequestValidationError` -- classes a host route does not raise -- so
    registering them app-wide costs the host nothing. Argument misuse
    (`ValueError`/`TypeError`) is deliberately *not* here; it is scoped to
    this package's own routes by `BinnacleAPIRoute`.

    Deliberately no catch-all for `BinnacleError`: an unmapped core error
    should surface as a 500 rather than be guessed into a 4xx that tells the
    client it did something wrong when it did not.
    """

    async def handle_binnacle_error(_: Request, exc: Exception) -> JSONResponse:
        # Safe: `add_exception_handler` below only ever routes here for the
        # `STATUS_BY_ERROR` keys themselves, all of which are `BinnacleError`
        # subclasses -- `exc`'s static type is `Exception` only because
        # Starlette's handler signature requires it.
        error_type = cast(type[BinnacleError], type(exc))
        return _problem(exc, STATUS_BY_ERROR[error_type])

    for error_type in STATUS_BY_ERROR:
        app.add_exception_handler(error_type, handle_binnacle_error)

    async def handle_request_validation_error(_: Request, exc: Exception) -> JSONResponse:
        # Safe: `add_exception_handler` below only ever routes here for
        # `RequestValidationError` itself.
        validation_exc = cast(RequestValidationError, exc)
        # `.errors()` can carry non-JSON-serializable values (e.g. in `ctx`) --
        # `jsonable_encoder` is FastAPI's own default handler's approach to that.
        errors = jsonable_encoder(validation_exc.errors())
        # `str(exc)` is deliberately not used for `detail` here: this FastAPI
        # version's `RequestValidationError.__str__` appends the server-side
        # endpoint's file path and line number, which a public error body must
        # not leak (GUIDELINES §9 "no secrets"). A summary built from the
        # per-field errors -- which are carried in full in `errors` below --
        # keeps `detail` informative without the leak.
        detail = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}" for error in errors
        )
        return _problem(exc, 422, detail=detail, extra={"errors": errors})

    app.add_exception_handler(RequestValidationError, handle_request_validation_error)
