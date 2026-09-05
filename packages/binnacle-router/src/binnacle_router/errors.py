"""Typed core errors as HTTP problem documents (RFC 7807)."""

import re
from typing import Final, cast

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

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


def _problem_type(exc: Exception) -> str:
    """A stable, snake_case identifier a client can branch on without parsing prose."""
    return (
        "https://binnacle.dev/problems/"
        + re.sub(r"(?<!^)(?=[A-Z])", "_", type(exc).__name__).lower()
    )


def _problem(exc: Exception, status: int) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        media_type="application/problem+json",
        content={
            "type": _problem_type(exc),
            "title": type(exc).__name__,
            "status": status,
            "detail": str(exc),
        },
    )


def install_error_handlers(app: FastAPI) -> None:
    """Register one handler per mapped core error, plus argument misuse.

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

    async def handle_argument_misuse(_: Request, exc: Exception) -> JSONResponse:
        return _problem(exc, 422)

    app.add_exception_handler(ValueError, handle_argument_misuse)
    app.add_exception_handler(TypeError, handle_argument_misuse)
