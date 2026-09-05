"""Typed core errors must become real HTTP semantics, not a blanket 500.

The distinction that matters: a missing id *in the URL* is 404, while an
invalid *value inside the request* (an unregistered domain name) is 422 --
they are different failures and a client branches on them differently."""

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from binnacle_core import (
    AuthorityViolation,
    DecisionNotFound,
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


def test_an_unmapped_error_is_not_silently_swallowed(http: TestClient, client: AsyncMock) -> None:
    """A core error nobody mapped must surface as 500, never as a misleading
    2xx or a wrong 4xx that a client would treat as its own fault."""
    client.domains.side_effect = RuntimeError("something unforeseen")
    with pytest.raises(RuntimeError):
        http.get("/binnacle/v1/domains")
