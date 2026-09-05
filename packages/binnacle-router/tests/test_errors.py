"""Typed core errors must become real HTTP semantics, not a blanket 500.

The distinction that matters: a missing id *in the URL* is 404, while an
invalid *value inside the request* (an unregistered domain name) is 422 --
they are different failures and a client branches on them differently."""

from json import JSONDecodeError
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, ValidationError

from binnacle_core import (
    AuthorityViolation,
    DecisionNotFound,
    EmbeddingDimensionMismatch,
    IdempotencyConflict,
    InactiveDomain,
    InvalidCursor,
    InvalidTransition,
    ItemAlreadyResolved,
    ItemNotFound,
    UnknownDomain,
)

CASES = [
    (UnknownDomain("no such domain"), 422),
    (InactiveDomain("deactivated"), 422),
    (DecisionNotFound("missing"), 404),
    (ItemNotFound("missing"), 404),
    (InvalidTransition("draft", "promote", "illegal"), 409),
    (ItemAlreadyResolved("already"), 409),
    (IdempotencyConflict("diverged"), 409),
    (InvalidCursor("malformed"), 400),
    (AuthorityViolation("agents cannot promote"), 403),
    (EmbeddingDimensionMismatch("expected 768, got 384"), 500),
    (ValueError("bad argument"), 422),
]


@pytest.mark.parametrize(("raised", "expected_status"), CASES)
def test_core_errors_map_to_http_status(
    http: TestClient, client: AsyncMock, raised: Exception, expected_status: int
) -> None:
    client.domains.side_effect = raised
    assert http.get("/binnacle/v1/domains").status_code == expected_status


def test_error_body_is_an_rfc7807_problem_document(http: TestClient, client: AsyncMock) -> None:
    client.domains.side_effect = AuthorityViolation("agents cannot promote")
    response = http.get("/binnacle/v1/domains")
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["status"] == 403
    assert body["title"] == "AuthorityViolation"
    assert "agents cannot promote" in body["detail"]
    assert body["type"].endswith("authority_violation")


def test_embedding_dimension_mismatch_body_is_an_rfc7807_problem_document(
    http: TestClient, client: AsyncMock
) -> None:
    """`EmbeddingDimensionMismatch` is mapped to 500 in `STATUS_BY_ERROR`, but
    until this test existed nothing asserted its problem-document body --
    indistinguishable at the wire from an unmapped 500 (see
    `test_an_unmapped_error_is_not_silently_swallowed` below), which really
    does propagate rather than returning a body at all. `POST
    /sweeps:backfill_embeddings` is the only endpoint that can raise this in
    practice, but the mapping itself lives in `errors.py` and is exercised
    through `/domains` here like every other case above -- the vehicle
    endpoint is incidental to what this test checks."""
    client.domains.side_effect = EmbeddingDimensionMismatch("expected 768, got 384")
    response = http.get("/binnacle/v1/domains")
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["status"] == 500
    assert body["title"] == "EmbeddingDimensionMismatch"
    assert "expected 768, got 384" in body["detail"]
    assert body["type"].endswith("embedding_dimension_mismatch")


def test_an_unmapped_error_is_not_silently_swallowed(http: TestClient, client: AsyncMock) -> None:
    """A core error nobody mapped must surface as 500, never as a misleading
    2xx or a wrong 4xx that a client would treat as its own fault."""
    client.domains.side_effect = RuntimeError("something unforeseen")
    with pytest.raises(RuntimeError):
        http.get("/binnacle/v1/domains")


DECISION_ID = "3fa85f64-5717-4562-b3fc-2c963f66afa6"

HOST_BUGS: list[Exception] = [
    ValueError("a host bug in the host's own code"),
    TypeError("unsupported operand type(s) for +: 'int' and 'NoneType'"),
    JSONDecodeError("Expecting value", "not json", 0),
]


@pytest.fixture()
def host_app(app: FastAPI) -> FastAPI:
    """The README's mounting recipe verbatim, plus a route the *host* owns.

    Mounting this package must not change how the host's own endpoints
    fail. Starlette dispatches exception handlers by MRO across every route
    in the app, so an app-global handler for a builtin like `ValueError`
    reaches routes that have nothing to do with binnacle."""

    @app.get("/host/boom")
    async def host_boom(which: int) -> None:
        raise HOST_BUGS[which]

    return app


@pytest.mark.parametrize("which", range(len(HOST_BUGS)))
def test_a_host_route_keeps_its_own_error_handling(host_app: FastAPI, which: int) -> None:
    """Mounting binnacle must not turn the host's own bugs into "the client's
    fault". A host `TypeError`, a `pydantic.ValidationError` (a `ValueError`
    subclass, carrying the host model's internal field names), and a
    `json.JSONDecodeError` (also a `ValueError`) must all propagate as the
    host's own unhandled 5xx -- not be converted to 422 with the exception
    text in the body, which would leak host internals and blind the host's
    5xx alerting."""
    with TestClient(host_app) as http, pytest.raises(type(HOST_BUGS[which])):
        http.get("/host/boom", params={"which": which})


def test_a_host_pydantic_validation_error_is_not_converted(app: FastAPI) -> None:
    """The concrete leak: `pydantic.ValidationError` is a `ValueError`
    subclass, so an app-global `ValueError` handler renders the host model's
    internal field names into a 422 body the host never meant to publish."""

    class HostInternalModel(BaseModel):
        secret_internal_field: int

    @app.get("/host/pydantic")
    async def host_pydantic() -> None:
        HostInternalModel()  # type: ignore[call-arg]

    with TestClient(app) as http, pytest.raises(ValidationError):
        http.get("/host/pydantic")


SUB_ROUTER_ROUTES = [
    ("decisions read", "relevant", "GET", "/binnacle/v1/decisions"),
    ("decisions write", "reactivate", "POST", f"/binnacle/v1/decisions/{DECISION_ID}:reactivate"),
    ("queue", "queue", "GET", "/binnacle/v1/queue"),
    ("registry", "domains", "GET", "/binnacle/v1/domains"),
    ("feeds", "changes", "GET", "/binnacle/v1/changes"),
    ("sweeps", "archive_stale", "POST", "/binnacle/v1/sweeps:archive_stale"),
]
"""One route per sub-router `make_router` composes -- the six of them are the
blast-radius question: a route class set only on the outer router would leave
all six unprotected."""


@pytest.mark.parametrize(("group", "method_name", "verb", "path"), SUB_ROUTER_ROUTES)
def test_argument_misuse_is_422_on_every_sub_router(
    http: TestClient,
    client: AsyncMock,
    group: str,
    method_name: str,
    verb: str,
    path: str,
) -> None:
    """`ValueError`/`TypeError` -> 422 is scoped to binnacle's own routes by a
    custom `APIRoute` class, and FastAPI does *not* propagate `route_class`
    through `include_router` -- so every one of the six sub-routers has to set
    it itself. One route per sub-router pins that none was missed."""
    getattr(client, method_name).side_effect = ValueError(f"bad argument in {group}")
    response = http.request(verb, path)
    assert response.status_code == 422, group
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["title"] == "ValueError"
    assert body["type"].endswith("value_error")
    assert body["detail"] == f"bad argument in {group}"
