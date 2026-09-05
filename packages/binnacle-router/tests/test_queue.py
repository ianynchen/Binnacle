"""Queue reads paginate like decisions; queue actions are the human-gated
promotion surface. Every one of the five actions must carry the resolved
actor through to the client (a Task 5 review found exactly this gap missing
on a prior endpoint) -- assertions below check `await_args` against
`human_actor` itself, not merely that some actor was passed.

Deviation from the brief's literal test text (declared per GUIDELINES §1.1,
matching the precedent already set in `test_decisions_write.py`): the actor
is taken as the `human_actor` fixture parameter rather than
`from tests.conftest import ...` -- the task's own standing ruling forbids
importing from `conftest.py` in a test module.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

from fastapi.testclient import TestClient

from binnacle_core import (
    Actor,
    Decision,
    InvalidResolution,
    NewDecision,
    Page,
    QueueItem,
    QueueItemView,
)

NEW_DECISION = {
    "domain": "architecture",
    "scenario": "how should transient failures be handled?",
    "outcome": "retry with exponential backoff, capped at 3 attempts",
    "reasoning": "avoids thundering herd on recovery",
    "source": "meridian",
}


def _queue_item(item_id: int = 1) -> QueueItem:
    return QueueItem(
        item_id=item_id,
        kind="promote",
        decision_id=uuid4(),
        target_id=None,
        proposed_by=Actor("agent", "meridian/sess-1"),
        proposed_at=datetime(2026, 9, 1, tzinfo=UTC),
        rationale="looks stable across three sprints",
        confidence=0.82,
        resolved=False,
    )


def _decision(actor: Actor) -> Decision:
    return Decision(
        decision_id=uuid4(),
        domain="architecture",
        tier="long_term",
        status="current",
        scenario="how should transient failures be handled?",
        outcome="retry with exponential backoff, capped at 3 attempts",
        reasoning="avoids thundering herd on recovery",
        source="meridian",
        recorded_by=actor,
        recorded_at=datetime(2026, 9, 5, tzinfo=UTC),
    )


def test_queue_paginates_like_decisions(http: TestClient, client: AsyncMock) -> None:
    client.queue.return_value = Page(items=[], next_cursor="next-page")
    body = http.get("/binnacle/v1/queue", params={"order": "shakiest", "limit": 10}).json()
    assert body["next_cursor"] == "next-page"
    assert client.queue.await_args.kwargs["order"] == "shakiest"
    assert client.queue.await_args.kwargs["limit"] == 10


def test_a_nonempty_page_of_queue_items_serializes_faithfully(
    http: TestClient, client: AsyncMock
) -> None:
    """The response model is `Page[QueueItemView]`, a nested generic wrapping
    a dataclass that itself wraps a dataclass (`QueueItem`) -- this must be
    asserted against the actual JSON body, not merely that the mock was
    called with the right arguments."""
    item = _queue_item(item_id=42)
    view = QueueItemView(
        item=item,
        domain="architecture",
        decision_confidence=0.91,
        age=timedelta(days=3),
    )
    client.queue.return_value = Page(items=[view], next_cursor=None)

    response = http.get("/binnacle/v1/queue")

    assert response.status_code == 200
    body = response.json()
    assert body["next_cursor"] is None
    assert len(body["items"]) == 1
    row = body["items"][0]
    assert row["domain"] == "architecture"
    assert row["decision_confidence"] == 0.91
    # Pydantic v2 (which FastAPI uses to serialize the `response_model`)
    # renders `timedelta` as an ISO 8601 duration string, not total seconds
    # -- pin the actual wire value a client consumes (verified by running
    # this test, not assumed).
    assert row["age"] == "P3D"
    assert row["item"]["item_id"] == 42
    assert row["item"]["kind"] == "promote"
    assert row["item"]["decision_id"] == str(item.decision_id)
    assert row["item"]["proposed_by"] == {"kind": "agent", "id": "meridian/sess-1"}
    assert row["item"]["resolved"] is False


def test_queue_kinds_list_param_is_parsed(http: TestClient, client: AsyncMock) -> None:
    """`kinds` is a `list[str]` query parameter; without an explicit
    `Query()` annotation FastAPI silently resolves repeated `kinds=` params
    to `None` (Task 4/5 finding) rather than a list."""
    client.queue.return_value = Page(items=[], next_cursor=None)
    http.get("/binnacle/v1/queue", params={"kinds": ["promote", "conflict"]})
    assert client.queue.await_args.kwargs["kinds"] == ["promote", "conflict"]


def test_queue_cursor_round_trips_verbatim(http: TestClient, client: AsyncMock) -> None:
    client.queue.return_value = Page(items=[], next_cursor=None)
    http.get("/binnacle/v1/queue", params={"after": "opaque-token-xyz"})
    assert client.queue.await_args.kwargs["after"] == "opaque-token-xyz"


def test_promote_passes_the_resolved_actor_and_returns_the_decision(
    http: TestClient, client: AsyncMock, human_actor: Actor
) -> None:
    decision = _decision(human_actor)
    client.promote.return_value = decision
    response = http.post(
        "/binnacle/v1/queue/7:promote",
        headers={"X-Actor-Kind": "human", "X-Actor-Id": "mallory"},
    )
    assert response.status_code == 200
    assert response.json()["decision_id"] == str(decision.decision_id)
    assert client.promote.await_args.args[0] == 7
    assert client.promote.await_args.kwargs["actor"] == human_actor


def test_decline_forwards_its_reason(
    http: TestClient, client: AsyncMock, human_actor: Actor
) -> None:
    client.decline.return_value = None
    response = http.post("/binnacle/v1/queue/7:decline", json={"reason": "style bikeshedding"})
    assert response.status_code == 200
    assert client.decline.await_args.args[0] == 7
    assert client.decline.await_args.kwargs["reason"] == "style bikeshedding"
    assert client.decline.await_args.kwargs["actor"] == human_actor


def test_decline_reason_is_optional(http: TestClient, client: AsyncMock) -> None:
    client.decline.return_value = None
    response = http.post("/binnacle/v1/queue/7:decline", json={})
    assert response.status_code == 200
    assert client.decline.await_args.kwargs["reason"] is None


def test_apply_passes_the_resolved_actor(
    http: TestClient, client: AsyncMock, human_actor: Actor
) -> None:
    client.apply_item.return_value = None
    response = http.post("/binnacle/v1/queue/9:apply")
    assert response.status_code == 200
    assert client.apply_item.await_args.args[0] == 9
    assert client.apply_item.await_args.kwargs["actor"] == human_actor


def test_dismiss_passes_the_reason_and_resolved_actor(
    http: TestClient, client: AsyncMock, human_actor: Actor
) -> None:
    client.dismiss_item.return_value = None
    response = http.post("/binnacle/v1/queue/3:dismiss", json={"reason": "noise"})
    assert response.status_code == 200
    assert client.dismiss_item.await_args.args[0] == 3
    assert client.dismiss_item.await_args.kwargs["reason"] == "noise"
    assert client.dismiss_item.await_args.kwargs["actor"] == human_actor


def test_resolve_conflict_accepts_winner_id_alone(http: TestClient, client: AsyncMock) -> None:
    client.resolve_conflict.return_value = None
    response = http.post("/binnacle/v1/queue/1:resolve_conflict", json={"winner_id": str(uuid4())})
    assert response.status_code == 200


def test_resolve_conflict_forwards_winner_id_and_reason_together(
    http: TestClient, client: AsyncMock
) -> None:
    """Core supports a long-term winner discarding a short-term loser with
    `winner_id` + `reason` together (`reason` becomes the discard reason --
    see `LifecycleEngine.resolve_conflict`, and
    `test_lt_winner_discards_st_loser` in binnacle-core's own suite). The
    router must not stand between the client and this legitimate shape by
    guessing at a request-shape rule core doesn't itself enforce -- both
    values must reach the client verbatim."""
    client.resolve_conflict.return_value = None
    winner_id = uuid4()
    response = http.post(
        "/binnacle/v1/queue/1:resolve_conflict",
        json={"winner_id": str(winner_id), "reason": "lt policy wins"},
    )
    assert response.status_code == 200
    kwargs = client.resolve_conflict.await_args.kwargs
    assert kwargs["winner_id"] == winner_id
    assert kwargs["reason"] == "lt policy wins"


def test_resolve_conflict_rejection_from_core_surfaces_as_409(
    http: TestClient, client: AsyncMock
) -> None:
    """Shapes core itself rejects (e.g. `winner_id` and `refined` together,
    or all three absent) are core's call to make, not the router's -- they
    surface as `InvalidResolution` mapped to a 409 problem document, single-
    sourced from core rather than duplicated as a router-level 422."""
    client.resolve_conflict.side_effect = InvalidResolution(
        "resolve_conflict accepts at most one of winner_id or refined"
    )
    response = http.post(
        "/binnacle/v1/queue/1:resolve_conflict",
        json={"winner_id": str(uuid4()), "refined": NEW_DECISION},
    )
    assert response.status_code == 409
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["status"] == 409
    assert body["title"] == "InvalidResolution"
    assert body["type"].endswith("invalid_resolution")


def test_resolve_conflict_accepts_refined_alone(http: TestClient, client: AsyncMock) -> None:
    client.resolve_conflict.return_value = None
    response = http.post("/binnacle/v1/queue/1:resolve_conflict", json={"refined": NEW_DECISION})
    assert response.status_code == 200
    kwargs = client.resolve_conflict.await_args.kwargs
    assert isinstance(kwargs["refined"], NewDecision)
    assert kwargs["winner_id"] is None
    assert kwargs["reason"] is None


def test_resolve_conflict_accepts_reason_alone(http: TestClient, client: AsyncMock) -> None:
    client.resolve_conflict.return_value = None
    response = http.post("/binnacle/v1/queue/1:resolve_conflict", json={"reason": "accepted as-is"})
    assert response.status_code == 200
    kwargs = client.resolve_conflict.await_args.kwargs
    assert kwargs["reason"] == "accepted as-is"
    assert kwargs["winner_id"] is None
    assert kwargs["refined"] is None


def test_resolve_conflict_passes_the_resolved_actor(
    http: TestClient, client: AsyncMock, human_actor: Actor
) -> None:
    client.resolve_conflict.return_value = None
    winner_id = uuid4()
    http.post("/binnacle/v1/queue/1:resolve_conflict", json={"winner_id": str(winner_id)})
    kwargs = client.resolve_conflict.await_args.kwargs
    assert client.resolve_conflict.await_args.args[0] == 1
    assert kwargs["actor"] == human_actor
    assert kwargs["winner_id"] == winner_id


def test_malformed_item_id_is_rejected_before_reaching_the_client(
    http: TestClient, client: AsyncMock
) -> None:
    response = http.post("/binnacle/v1/queue/not-an-int:promote")
    assert response.status_code == 422
    client.promote.assert_not_awaited()


def test_custom_action_suffix_does_not_bleed_into_the_parsed_item_id(
    http: TestClient, client: AsyncMock, human_actor: Actor
) -> None:
    """`:apply` sits directly after the `{item_id}` path parameter -- this
    must resolve to the route (not 404) and hand the client a clean `int`,
    not the string `"9:apply"`."""
    client.apply_item.return_value = None
    response = http.post("/binnacle/v1/queue/9:apply")
    assert response.status_code == 200
    assert client.apply_item.await_args.args[0] == 9
    assert isinstance(client.apply_item.await_args.args[0], int)
