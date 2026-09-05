"""Reads translate query parameters into client arguments and nothing more."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

from fastapi.testclient import TestClient

from binnacle_core import Actor, CompactDecision, Decision, Page, Ref


def _page() -> Page[CompactDecision]:
    return Page(items=[], next_cursor=None)


def test_a_nonempty_page_of_compact_decisions_serializes_faithfully(
    http: TestClient, client: AsyncMock
) -> None:
    """Carried risk from Task 2: FastAPI's serialization of binnacle-core's
    frozen dataclasses has only been verified against a flat 3-field
    primitive dataclass (DomainRecord). GET /decisions is the first endpoint
    returning a generic, nested Page[CompactDecision] -- this asserts the
    actual JSON body round-trips field names and values, including
    next_cursor, before anything else in this task is built on top of it."""
    decision_id = uuid4()
    decision = CompactDecision(
        id=decision_id,
        domain="architecture",
        tier="long_term",
        status="current",
        outcome_truncated="Use PostgreSQL for the decision store.",
        subject_refs=[
            Ref(role="subject", kind="component", identifier="portolan-ingest", note="primary")
        ],
    )
    client.relevant.return_value = Page(items=[decision], next_cursor="opaque-token-xyz")

    response = http.get("/binnacle/v1/decisions")

    assert response.status_code == 200
    body = response.json()
    assert body["next_cursor"] == "opaque-token-xyz"
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["id"] == str(decision_id)
    assert item["domain"] == "architecture"
    assert item["tier"] == "long_term"
    assert item["status"] == "current"
    assert item["outcome_truncated"] == "Use PostgreSQL for the decision store."
    assert item["subject_refs"] == [
        {
            "role": "subject",
            "kind": "component",
            "identifier": "portolan-ingest",
            "note": "primary",
        }
    ]


def test_filters_and_sort_reach_the_client_unchanged(http: TestClient, client: AsyncMock) -> None:
    client.relevant.return_value = _page()
    http.get(
        "/binnacle/v1/decisions",
        params={
            "domains": ["architecture", "testing"],
            "status": ["current"],
            "tier": "long_term",
            "sort": "last_touched_at",
            "order": "asc",
            "limit": 25,
            "subject_kind": "component",
            "subject_identifier": "portolan-ingest",
        },
    )
    kwargs = client.relevant.await_args.kwargs
    assert kwargs["domains"] == ["architecture", "testing"]
    assert kwargs["tier"] == "long_term"
    assert kwargs["sort"] == "last_touched_at"
    assert kwargs["order"] == "asc"
    assert kwargs["limit"] == 25
    assert kwargs["subject"] == ("component", "portolan-ingest")


def test_projection_full_returns_untruncated_decisions(http: TestClient, client: AsyncMock) -> None:
    """`projection=full` reaches the client verbatim and the endpoint's
    Page[CompactDecision] | Page[Decision] return type serializes the full
    shape too, not just the default compact one."""
    decision = Decision(
        decision_id=uuid4(),
        domain="architecture",
        tier="long_term",
        status="current",
        scenario="s",
        outcome="the untruncated outcome",
        reasoning="r",
        source="src",
        recorded_by=Actor("human", "alice"),
        recorded_at=datetime(2021, 3, 14, 9, 22, 11, tzinfo=UTC),
    )
    client.relevant.return_value = Page(items=[decision], next_cursor=None)

    response = http.get("/binnacle/v1/decisions", params={"projection": "full"})

    assert response.status_code == 200
    assert client.relevant.await_args.kwargs["projection"] == "full"
    item = response.json()["items"][0]
    assert item["decision_id"] == str(decision.decision_id)
    assert item["outcome"] == "the untruncated outcome"


def test_half_a_subject_pair_is_rejected(http: TestClient, client: AsyncMock) -> None:
    """A subject is a (kind, identifier) pair; half of one is meaningless and
    would otherwise be silently dropped, returning a wider result set than asked for."""
    response = http.get("/binnacle/v1/decisions", params={"subject_kind": "component"})
    assert response.status_code == 422
    client.relevant.assert_not_awaited()


def test_the_cursor_round_trips_verbatim(http: TestClient, client: AsyncMock) -> None:
    client.relevant.return_value = Page(items=[], next_cursor="opaque-token-xyz")
    body = http.get("/binnacle/v1/decisions").json()
    assert body["next_cursor"] == "opaque-token-xyz"

    http.get("/binnacle/v1/decisions", params={"after": "opaque-token-xyz"})
    assert client.relevant.await_args.kwargs["after"] == "opaque-token-xyz"


def test_count_rejects_pagination_parameters(http: TestClient, client: AsyncMock) -> None:
    """sort/after/limit cannot affect a count; accepting them would imply otherwise."""
    client.relevant_count.return_value = 7
    assert http.get("/binnacle/v1/decisions/count").json() == {"count": 7}
    assert (
        http.get("/binnacle/v1/decisions/count", params={"sort": "recorded_at"}).status_code == 422
    )


def test_batch_get_takes_a_body_not_a_query_string(http: TestClient, client: AsyncMock) -> None:
    """200 UUIDs is ~7.4 KB of URL, past common limits."""
    client.get_many.return_value = []
    ids = [str(uuid4()) for _ in range(3)]
    assert http.post("/binnacle/v1/decisions:batch_get", json={"ids": ids}).status_code == 200
    assert [str(i) for i in client.get_many.await_args.args[0]] == ids


def test_by_source_forwards_source_and_filters(http: TestClient, client: AsyncMock) -> None:
    """`by_source(source, **filters)` accepts `status`/`tier`/`limit` as
    keyword filters (see `StorePort.by_source`'s docstring) -- unsupplied
    ones must be omitted rather than passed as `None`, since the store
    distinguishes "not given" (its own default) from an explicit `None`."""
    client.by_source.return_value = []
    http.get(
        "/binnacle/v1/decisions/by_source",
        params={"source": "meridian", "status": ["current"], "tier": "long_term", "limit": 10},
    )
    args, kwargs = client.by_source.await_args
    assert args == ("meridian",)
    assert kwargs == {"status": ["current"], "tier": "long_term", "limit": 10}


def test_by_source_omits_unsupplied_filters(http: TestClient, client: AsyncMock) -> None:
    client.by_source.return_value = []
    http.get("/binnacle/v1/decisions/by_source", params={"source": "meridian"})
    args, kwargs = client.by_source.await_args
    assert args == ("meridian",)
    assert kwargs == {}


def test_history_passes_the_path_id(http: TestClient, client: AsyncMock) -> None:
    decision_id = uuid4()
    client.history.return_value = None
    http.get(f"/binnacle/v1/decisions/{decision_id}/history")
    assert client.history.await_args.args[0] == decision_id


def test_as_of_is_parsed_as_a_datetime(http: TestClient, client: AsyncMock) -> None:
    client.relevant.return_value = _page()
    http.get("/binnacle/v1/decisions", params={"as_of": "2021-03-14T09:22:11Z"})
    assert client.relevant.await_args.kwargs["as_of"] == datetime(
        2021, 3, 14, 9, 22, 11, tzinfo=UTC
    )
