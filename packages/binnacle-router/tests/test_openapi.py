"""The published OpenAPI must describe the error bodies this package really
sends.

A generated client builds its 422 deserializer from this document. FastAPI's
stock declaration -- `application/json` carrying `HTTPValidationError`, whose
`detail` is an *array* -- describes a body this package never produces: every
422 here is `application/problem+json` whose `detail` is a *string*, with the
per-field errors carried in an `errors` extension member (REQUIREMENTS FR-5.2,
FR-5.4). A client generated from the stock declaration breaks on the first
validation error it sees."""

from typing import Any
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from binnacle_core import UnknownDomain
from binnacle_router.errors import PROBLEM_RESPONSES, ProblemDocument

PROBLEM_MEDIA_TYPE = "application/problem+json"


def _operations(app: FastAPI) -> dict[str, dict[str, Any]]:
    schema = app.openapi()
    return {
        f"{method.upper()} {path}": operation
        for path, methods in schema["paths"].items()
        for method, operation in methods.items()
    }


def test_every_operation_declares_the_problem_document_for_422(app: FastAPI) -> None:
    """Package-wide, not per-route: `make_router` sets `responses` on the
    router it returns, and FastAPI propagates that through `include_router` to
    every sub-router's operations."""
    operations = _operations(app)
    assert operations, "the router must publish operations to describe"
    for name, operation in operations.items():
        content = operation["responses"]["422"]["content"]
        assert list(content) == [PROBLEM_MEDIA_TYPE], name
        assert content[PROBLEM_MEDIA_TYPE]["schema"]["title"] == "ProblemDocument", name


def test_no_operation_still_advertises_the_stock_validation_error(app: FastAPI) -> None:
    """`HTTPValidationError`'s array-valued `detail` is the exact shape this
    package does not send; nothing may reference it, under any status code."""
    schema = app.openapi()
    assert "HTTPValidationError" not in schema.get("components", {}).get("schemas", {})
    assert "HTTPValidationError" not in str(schema)


def test_the_declared_422_schema_is_the_problem_document_model(app: FastAPI) -> None:
    """The declaration is generated from the same model the assertions below
    validate real bodies against, so the document cannot drift from the code
    by being edited in only one of the two places."""
    declared = PROBLEM_RESPONSES[422]["content"][PROBLEM_MEDIA_TYPE]["schema"]
    assert declared == ProblemDocument.model_json_schema()

    published = _operations(app)["GET /binnacle/v1/domains"]["responses"]["422"]
    schema = published["content"][PROBLEM_MEDIA_TYPE]["schema"]
    # FastAPI strips null-valued keys from the whole document, so `errors`'
    # `"default": null` does not survive into it; nothing else may differ.
    assert schema["properties"]["errors"] == {
        key: value for key, value in declared["properties"]["errors"].items() if value is not None
    }
    assert {**schema, "properties": {}} == {**declared, "properties": {}}
    assert schema["properties"]["detail"]["type"] == "string", (
        "the shape the stock HTTPValidationError got wrong: detail is a string, not an array"
    )


def test_a_real_422_body_validates_against_the_declared_schema(
    http: TestClient, client: AsyncMock
) -> None:
    """All three routes to a 422 -- the router's own `paired()` misuse, a
    typed core error, and FastAPI's request validation -- must produce a body
    the published schema actually describes. The third is the one that used to
    be misdescribed: its `detail` is a string and its field errors live in
    `errors`, not in `detail`."""
    misuse = http.get("/binnacle/v1/changes", params={"actor_kind": "human"})
    assert misuse.status_code == 422
    assert misuse.headers["content-type"].startswith(PROBLEM_MEDIA_TYPE)
    assert isinstance(ProblemDocument.model_validate(misuse.json()).detail, str)

    client.domains.side_effect = UnknownDomain("no such domain")
    core_error = http.get("/binnacle/v1/domains")
    assert core_error.status_code == 422
    assert ProblemDocument.model_validate(core_error.json()).errors is None

    invalid = http.get("/binnacle/v1/decisions/count", params={"sort": "recorded_at"})
    assert invalid.status_code == 422
    validated = ProblemDocument.model_validate(invalid.json())
    assert isinstance(validated.detail, str)
    assert validated.errors is not None and validated.errors[0]["loc"] == ["query", "sort"]
