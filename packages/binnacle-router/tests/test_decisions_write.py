"""Writes carry an attested actor and translate one call each.

Deviation from the brief's literal test text (declared per GUIDELINES §1.1):
the brief shows `from tests.conftest import HUMAN` and `client.record.return_value
= None`. Importing from `tests.conftest` is a defect the task instructions
flagged explicitly -- the actor is taken as the `human_actor` fixture
parameter instead. Mocking `.record`/`.record_long_term`/`.promote_refined`
with `return_value = None` is also wrong to keep: those client methods return
a `Decision`, the corresponding endpoints declare `-> Decision`, and FastAPI's
response validation rejects `None` against that model -- so these tests
return a real, fully-populated `Decision` instead (matching the Task 4
finding that mocks must exercise real response serialization, not `None` or
a bare `MagicMock`).
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

from fastapi.testclient import TestClient

from binnacle_core import Actor, Decision, NewDecision

NEW_DECISION = {
    "domain": "architecture",
    "scenario": "how should transient failures be handled?",
    "outcome": "retry with exponential backoff, capped at 3 attempts",
    "reasoning": "avoids thundering herd on recovery",
    "source": "meridian",
}


def _decision(actor: Actor) -> Decision:
    return Decision(
        decision_id=uuid4(),
        domain="architecture",
        tier="short_term",
        status="current",
        scenario="how should transient failures be handled?",
        outcome="retry with exponential backoff, capped at 3 attempts",
        reasoning="avoids thundering herd on recovery",
        source="meridian",
        recorded_by=actor,
        recorded_at=datetime(2026, 9, 5, tzinfo=UTC),
    )


def test_record_passes_the_resolved_actor_not_a_client_supplied_one(
    http: TestClient, client: AsyncMock, human_actor: Actor
) -> None:
    """The actor comes from the host's resolver. If a client-supplied value
    could reach the client, any caller could self-declare `human` and walk
    through the promotion gate."""
    client.record.return_value = _decision(human_actor)
    http.post(
        "/binnacle/v1/decisions",
        json=NEW_DECISION,
        headers={"X-Actor-Kind": "human", "X-Actor-Id": "mallory"},
    )
    assert client.record.await_args.kwargs["actor"] == human_actor


def test_record_serializes_the_returned_decision(
    http: TestClient, client: AsyncMock, human_actor: Actor
) -> None:
    decision = _decision(human_actor)
    client.record.return_value = decision
    response = http.post("/binnacle/v1/decisions", json=NEW_DECISION)
    assert response.status_code == 200
    body = response.json()
    assert body["decision_id"] == str(decision.decision_id)
    assert body["tier"] == "short_term"


def test_record_forwards_the_posted_decision_body(
    http: TestClient, client: AsyncMock, human_actor: Actor
) -> None:
    """The `NewDecision` the client receives must be the one the request
    actually carried, not merely *some* `NewDecision` -- a swapped-in
    placeholder (e.g. `record(None, ...)`) would otherwise pass every other
    assertion here."""
    client.record.return_value = _decision(human_actor)
    http.post("/binnacle/v1/decisions", json=NEW_DECISION)
    assert client.record.await_args.args[0] == NewDecision(**NEW_DECISION)


def test_record_long_term_reaches_record_long_term_not_record(
    http: TestClient, client: AsyncMock, human_actor: Actor
) -> None:
    client.record_long_term.return_value = _decision(human_actor)
    http.post("/binnacle/v1/decisions/long_term", json=NEW_DECISION)
    client.record_long_term.assert_awaited_once()
    client.record.assert_not_awaited()
    assert client.record_long_term.await_args.kwargs["actor"] == human_actor
    assert client.record_long_term.await_args.args[0] == NewDecision(**NEW_DECISION)


def test_promote_refined_passes_source_ids_and_the_resolved_actor(
    http: TestClient, client: AsyncMock, human_actor: Actor
) -> None:
    source_ids = [uuid4(), uuid4()]
    client.promote_refined.return_value = _decision(human_actor)
    http.post(
        "/binnacle/v1/decisions:promote_refined",
        json={"source_ids": [str(i) for i in source_ids], "refined": NEW_DECISION},
    )
    kwargs = client.promote_refined.await_args.kwargs
    assert client.promote_refined.await_args.args[0] == source_ids
    assert client.promote_refined.await_args.args[1] == NewDecision(**NEW_DECISION)
    assert kwargs["actor"] == human_actor


def test_relationship_direction_is_path_id_supersedes_target(
    http: TestClient, client: AsyncMock, human_actor: Actor
) -> None:
    """Path id is the `from` side, target_id the `to` side -- matching
    supersede(new_id, old_id) and the links table. Backwards here is data
    corruption, not a cosmetic slip."""
    new_id, old_id = uuid4(), uuid4()
    client.supersede.return_value = None
    response = http.post(
        f"/binnacle/v1/decisions/{new_id}/relationships",
        json={"kind": "SUPERSEDES", "target_id": str(old_id)},
    )
    assert response.status_code == 200
    assert client.supersede.await_args.args[:2] == (new_id, old_id)
    assert client.supersede.await_args.kwargs["actor"] == human_actor


def test_supplements_routes_to_supplement_not_supersede(
    http: TestClient, client: AsyncMock, human_actor: Actor
) -> None:
    """Path id is the `from` side, target_id the `to` side -- matching
    supplement(new_id, old_id) -- exactly like the `SUPERSEDES` sibling
    above. Without pinning the ids here, a reversed call
    (`supplement(target_id, decision_id, ...)`) is data corruption that this
    test would not catch."""
    new_id, old_id = uuid4(), uuid4()
    client.supplement.return_value = None
    response = http.post(
        f"/binnacle/v1/decisions/{new_id}/relationships",
        json={"kind": "SUPPLEMENTS", "target_id": str(old_id)},
    )
    assert response.status_code == 200
    client.supplement.assert_awaited_once()
    client.supersede.assert_not_awaited()
    assert client.supplement.await_args.args[:2] == (new_id, old_id)
    assert client.supplement.await_args.kwargs["actor"] == human_actor


def test_an_unknown_relationship_kind_is_rejected(http: TestClient, client: AsyncMock) -> None:
    response = http.post(
        f"/binnacle/v1/decisions/{uuid4()}/relationships",
        json={"kind": "CONFLICTS_WITH", "target_id": str(uuid4())},
    )
    assert response.status_code == 422, "only human-curatable kinds are settable here"
    client.supersede.assert_not_awaited()
    client.supplement.assert_not_awaited()


def test_recommend_decision_id_is_parsed_without_the_suffix_bleeding_in(
    http: TestClient, client: AsyncMock, human_actor: Actor
) -> None:
    """`:recommend` sits directly after the `{decision_id}` path parameter --
    this must resolve to the route (not 404) and hand the client a clean
    UUID, not `"<uuid>:recommend"`."""
    decision_id = uuid4()
    client.recommend.return_value = 42
    response = http.post(f"/binnacle/v1/decisions/{decision_id}:recommend", json={"reason": "x"})
    assert response.status_code == 200
    assert client.recommend.await_args.args[0] == decision_id
    assert client.recommend.await_args.kwargs["actor"] == human_actor


def test_recommend_returns_the_new_queue_item_id(http: TestClient, client: AsyncMock) -> None:
    client.recommend.return_value = 42
    body = http.post(
        f"/binnacle/v1/decisions/{uuid4()}:recommend", json={"reason": "policy"}
    ).json()
    assert body == {"item_id": 42}


def test_recommend_returns_null_item_id_when_nothing_was_filed(
    http: TestClient, client: AsyncMock
) -> None:
    """`recommend()` returns `None` when no new queue item was created (e.g.
    an existing pending recommendation already covers it) -- the endpoint
    must surface that, not coerce it into a truthy id."""
    client.recommend.return_value = None
    body = http.post(f"/binnacle/v1/decisions/{uuid4()}:recommend", json={"reason": "x"}).json()
    assert body == {"item_id": None}


def test_recommend_reason_is_optional(http: TestClient, client: AsyncMock) -> None:
    client.recommend.return_value = None
    response = http.post(f"/binnacle/v1/decisions/{uuid4()}:recommend", json={})
    assert response.status_code == 200
    assert client.recommend.await_args.kwargs["reason"] is None


def test_discard_passes_the_reason_and_resolved_actor(
    http: TestClient, client: AsyncMock, human_actor: Actor
) -> None:
    decision_id = uuid4()
    client.discard.return_value = None
    response = http.post(
        f"/binnacle/v1/decisions/{decision_id}:discard", json={"reason": "duplicate"}
    )
    assert response.status_code == 200
    assert client.discard.await_args.args[0] == decision_id
    assert client.discard.await_args.kwargs["reason"] == "duplicate"
    assert client.discard.await_args.kwargs["actor"] == human_actor


def test_reactivate_does_not_shadow_or_get_shadowed_by_read_routes(
    http: TestClient, client: AsyncMock, human_actor: Actor
) -> None:
    """`:reactivate` shares the `{decision_id}` prefix with the read router's
    `GET /decisions/{decision_id}/history` -- this must resolve to
    `reactivate`, not 404 or the history endpoint."""
    decision_id = uuid4()
    client.reactivate.return_value = None
    response = http.post(f"/binnacle/v1/decisions/{decision_id}:reactivate")
    assert response.status_code == 200
    client.reactivate.assert_awaited_once_with(decision_id, actor=human_actor)
    client.history.assert_not_awaited()
