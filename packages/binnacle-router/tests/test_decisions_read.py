"""Reads translate query parameters into client arguments and nothing more."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from binnacle_core import (
    Actor,
    CompactDecision,
    Decision,
    HistoryRecord,
    Link,
    Page,
    Ref,
    Transition,
)


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


def test_evidence_text_expiring_before_and_include_archived_reach_the_client(
    http: TestClient, client: AsyncMock
) -> None:
    """The `subject`/`sort`/`limit` filters above are not the only ones
    `_FilterFields` declares -- `evidence`, `text`, `expiring_before`, and
    `include_archived` reach `relevant()` too, each with its own value, not
    merely a truthy default."""
    client.relevant.return_value = _page()
    http.get(
        "/binnacle/v1/decisions",
        params={
            "evidence_kind": "commit",
            "evidence_identifier": "abc123",
            "text": "retry policy",
            "expiring_before": "2027-01-01T00:00:00Z",
            "include_archived": True,
        },
    )
    kwargs = client.relevant.await_args.kwargs
    assert kwargs["evidence"] == ("commit", "abc123")
    assert kwargs["text"] == "retry policy"
    assert kwargs["expiring_before"] == datetime(2027, 1, 1, tzinfo=UTC)
    assert kwargs["include_archived"] is True


def test_half_an_evidence_pair_is_rejected(http: TestClient, client: AsyncMock) -> None:
    """Mirrors `test_half_a_subject_pair_is_rejected` -- `evidence` is
    likewise a (kind, identifier) pair guarded by the same `paired()`
    helper, and until this test existed nothing pinned that guard for
    `evidence` specifically."""
    response = http.get("/binnacle/v1/decisions", params={"evidence_kind": "commit"})
    assert response.status_code == 422
    assert (
        response.json()["detail"]
        == "evidence_kind and evidence_identifier must be supplied together"
    )
    client.relevant.assert_not_awaited()


def test_all_filters_reach_relevant_count(http: TestClient, client: AsyncMock) -> None:
    """`GET /decisions/count` shares `_FilterFields` with `GET /decisions`
    and forwards all nine of them to `relevant_count()` -- pinned together
    so that dropping any one of them (e.g. a bare `relevant_count()`) fails."""
    client.relevant_count.return_value = 3
    http.get(
        "/binnacle/v1/decisions/count",
        params={
            "domains": ["architecture"],
            "subject_kind": "component",
            "subject_identifier": "portolan-ingest",
            "evidence_kind": "commit",
            "evidence_identifier": "abc123",
            "status": ["current"],
            "tier": "long_term",
            "as_of": "2021-03-14T09:22:11Z",
            "expiring_before": "2027-01-01T00:00:00Z",
            "text": "retry policy",
            "include_archived": True,
        },
    )
    kwargs = client.relevant_count.await_args.kwargs
    assert kwargs["domains"] == ["architecture"]
    assert kwargs["subject"] == ("component", "portolan-ingest")
    assert kwargs["evidence"] == ("commit", "abc123")
    assert kwargs["status"] == ["current"]
    assert kwargs["tier"] == "long_term"
    assert kwargs["as_of"] == datetime(2021, 3, 14, 9, 22, 11, tzinfo=UTC)
    assert kwargs["expiring_before"] == datetime(2027, 1, 1, tzinfo=UTC)
    assert kwargs["text"] == "retry policy"
    assert kwargs["include_archived"] is True


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
    would otherwise be silently dropped, returning a wider result set than asked for.

    The message must name the parameters this endpoint actually declares, so a
    client can act on it (REQUIREMENTS FR-4.5)."""
    response = http.get("/binnacle/v1/decisions", params={"subject_kind": "component"})
    assert response.status_code == 422
    assert (
        response.json()["detail"] == "subject_kind and subject_identifier must be supplied together"
    )
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
    response = http.get("/binnacle/v1/decisions/count", params={"sort": "recorded_at"})
    assert response.status_code == 422


def test_count_rejects_pagination_parameters_as_a_problem_document(
    http: TestClient, client: AsyncMock
) -> None:
    """FastAPI's own request-validation errors -- not just binnacle_core's typed
    errors -- must still come back as RFC 7807 `application/problem+json`
    (project-wide constraint), field-level detail included rather than
    discarded."""
    response = http.get("/binnacle/v1/decisions/count", params={"sort": "recorded_at"})
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["status"] == 422
    assert body["title"] == "RequestValidationError"
    assert body["type"].endswith("request_validation_error")
    assert isinstance(body["detail"], str) and body["detail"]
    assert body["errors"][0]["loc"] == ["query", "sort"]
    assert body["errors"][0]["msg"]


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


def test_history_passes_the_path_id_and_the_record_round_trips(
    http: TestClient, client: AsyncMock
) -> None:
    """`GET /decisions/{id}/history` declares `-> HistoryRecord`; FastAPI's
    response-model validation must accept a real, fully populated record --
    including the nested `Decision` and `Actor` -- and the JSON body must
    carry every field through, not merely return 200."""
    decision_id = uuid4()
    predecessor_id = uuid4()
    successor_id = uuid4()
    supplement_id = uuid4()
    conflict_id = uuid4()
    recorded_by = Actor("human", "alice")
    recorded_at = datetime(2021, 3, 14, 9, 22, 11, tzinfo=UTC)

    def _decision(d_id: UUID) -> Decision:
        return Decision(
            decision_id=d_id,
            domain="architecture",
            tier="long_term",
            status="current",
            scenario="s",
            outcome="o",
            reasoning="r",
            source="src",
            recorded_by=recorded_by,
            recorded_at=recorded_at,
        )

    history = HistoryRecord(
        decision=_decision(decision_id),
        transitions=[
            Transition(
                transition_id=1,
                decision_id=decision_id,
                action="recorded",
                actor=recorded_by,
                at=recorded_at,
                reason="initial record",
                new_status="current",
                payload=None,
            )
        ],
        links=[Link(from_id=decision_id, to_id=predecessor_id, kind="SUPERSEDES")],
        predecessors=[_decision(predecessor_id)],
        successors=[_decision(successor_id)],
        supplements=[_decision(supplement_id)],
        conflicts=[_decision(conflict_id)],
    )
    client.history.return_value = history

    response = http.get(f"/binnacle/v1/decisions/{decision_id}/history")

    assert response.status_code == 200
    assert client.history.await_args.args[0] == decision_id

    body = response.json()
    assert body["decision"]["decision_id"] == str(decision_id)
    assert body["decision"]["recorded_by"] == {"kind": "human", "id": "alice"}
    assert body["transitions"][0]["action"] == "recorded"
    assert body["transitions"][0]["actor"] == {"kind": "human", "id": "alice"}
    assert body["links"][0] == {
        "from_id": str(decision_id),
        "to_id": str(predecessor_id),
        "kind": "SUPERSEDES",
    }
    assert body["predecessors"][0]["decision_id"] == str(predecessor_id)
    assert body["successors"][0]["decision_id"] == str(successor_id)
    assert body["supplements"][0]["decision_id"] == str(supplement_id)
    assert body["conflicts"][0]["decision_id"] == str(conflict_id)


def test_as_of_is_parsed_as_a_datetime(http: TestClient, client: AsyncMock) -> None:
    client.relevant.return_value = _page()
    http.get("/binnacle/v1/decisions", params={"as_of": "2021-03-14T09:22:11Z"})
    assert client.relevant.await_args.kwargs["as_of"] == datetime(
        2021, 3, 14, 9, 22, 11, tzinfo=UTC
    )


@pytest.mark.parametrize("limit", [0, -5])
def test_out_of_range_limit_is_rejected_as_a_problem_document(
    http: TestClient, client: AsyncMock, limit: int
) -> None:
    """`limit=0` is the subtler of the two: the keyset pagination trick in
    `postgres_store.py` (`params["limit"] = limit + 1`) turns it into
    `LIMIT 1`, so a client paging on `next_cursor` would loop forever on
    empty pages. `limit=-5` reaches PostgreSQL as a negative `LIMIT`, which
    raises and would otherwise surface as an unmapped 500 (`errors.py`
    registers no catch-all). Neither reaches `binnacle.relevant()` at all
    once `DecisionsQuery.limit` is constrained to `ge=1`."""
    response = http.get("/binnacle/v1/decisions", params={"limit": limit})
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["status"] == 422
    assert body["errors"][0]["loc"] == ["query", "limit"]
    client.relevant.assert_not_awaited()


def test_limit_of_one_is_accepted(http: TestClient, client: AsyncMock) -> None:
    """Pins the boundary on the correct side: `1` is the smallest
    meaningful page size and must not be rejected alongside `0`/`-5`."""
    client.relevant.return_value = _page()
    response = http.get("/binnacle/v1/decisions", params={"limit": 1})
    assert response.status_code == 200
    assert client.relevant.await_args.kwargs["limit"] == 1


@pytest.mark.parametrize("limit", [0, -5])
def test_by_source_out_of_range_limit_is_rejected_as_a_problem_document(
    http: TestClient, client: AsyncMock, limit: int
) -> None:
    """`GET /decisions/by_source`'s `limit` is a bare, optional query
    parameter (not a pydantic model field like `DecisionsQuery.limit`
    above) -- constrained the same way via `Query(ge=1)` rather than
    `Field`, but must reject the same out-of-range values."""
    response = http.get(
        "/binnacle/v1/decisions/by_source", params={"source": "meridian", "limit": limit}
    )
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["status"] == 422
    client.by_source.assert_not_awaited()


def test_by_source_limit_of_one_is_accepted(http: TestClient, client: AsyncMock) -> None:
    client.by_source.return_value = []
    response = http.get(
        "/binnacle/v1/decisions/by_source", params={"source": "meridian", "limit": 1}
    )
    assert response.status_code == 200
    assert client.by_source.await_args.kwargs["limit"] == 1


def test_by_source_limit_none_remains_valid(http: TestClient, client: AsyncMock) -> None:
    """`limit` is optional on this endpoint -- an absent value must still
    omit the filter entirely (per `test_by_source_omits_unsupplied_filters`
    above), not be forced into range by the new `ge=1` constraint."""
    client.by_source.return_value = []
    response = http.get("/binnacle/v1/decisions/by_source", params={"source": "meridian"})
    assert response.status_code == 200
    assert "limit" not in client.by_source.await_args.kwargs
