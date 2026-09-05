"""The changes feed, precedent search, and export -- all unattributed reads:
none of these three endpoints takes an actor dependency (see `feeds_router`'s
docstring)."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

from fastapi.testclient import TestClient

from binnacle_core import Actor, CompactDecision, PrecedentHit, Ref, Transition


def _transition(decision_id: object, actor: Actor) -> Transition:
    return Transition(
        transition_id=1,
        decision_id=decision_id,  # type: ignore[arg-type]
        action="recorded",
        actor=actor,
        at=datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC),
        reason="initial record",
        new_status="current",
        payload=None,
    )


def _compact_decision(decision_id: object) -> CompactDecision:
    return CompactDecision(
        id=decision_id,  # type: ignore[arg-type]
        domain="architecture",
        tier="long_term",
        status="current",
        outcome_truncated="Use PostgreSQL for the decision store.",
        subject_refs=[
            Ref(role="subject", kind="component", identifier="portolan-ingest", note=None)
        ],
    )


def test_changes_pairs_are_wrapped_in_named_fields(http: TestClient, client: AsyncMock) -> None:
    """`Binnacle.changes()` returns `(Transition, CompactDecision)` tuples,
    which would serialize as a bare two-element JSON array, forcing a client
    to index by position. The endpoint wraps each pair as
    `{"transition": ..., "decision": ...}` instead -- this asserts both
    halves serialize fully, not merely that the wrapper keys exist."""
    decision_id = uuid4()
    actor = Actor("agent", "meridian/sess-1")
    client.changes.return_value = [
        (_transition(decision_id, actor), _compact_decision(decision_id))
    ]

    response = http.get("/binnacle/v1/changes")

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    entry = body[0]
    assert entry["transition"]["transition_id"] == 1
    assert entry["transition"]["decision_id"] == str(decision_id)
    assert entry["transition"]["action"] == "recorded"
    assert entry["transition"]["actor"] == {"kind": "agent", "id": "meridian/sess-1"}
    # Pydantic v2 renders a UTC `datetime` with a `Z` suffix, not `+00:00`.
    assert entry["transition"]["at"] == "2026-09-01T12:00:00Z"
    assert entry["transition"]["reason"] == "initial record"
    assert entry["transition"]["new_status"] == "current"
    assert entry["transition"]["payload"] is None
    assert entry["decision"]["id"] == str(decision_id)
    assert entry["decision"]["domain"] == "architecture"
    assert entry["decision"]["tier"] == "long_term"
    assert entry["decision"]["status"] == "current"
    assert entry["decision"]["outcome_truncated"] == "Use PostgreSQL for the decision store."
    assert entry["decision"]["subject_refs"] == [
        {"role": "subject", "kind": "component", "identifier": "portolan-ingest", "note": None}
    ]

    assert http.get("/binnacle/v1/changes", params={"actor_kind": "human"}).status_code == 422


def test_changes_forwards_since_actions_limit_and_after_id(
    http: TestClient, client: AsyncMock
) -> None:
    client.changes.return_value = []
    http.get(
        "/binnacle/v1/changes",
        params={
            "since": "2026-09-01T00:00:00Z",
            "actions": ["recorded", "promoted"],
            "limit": 10,
            "after_id": 5,
        },
    )
    kwargs = client.changes.await_args.kwargs
    assert kwargs["since"] == datetime(2026, 9, 1, tzinfo=UTC)
    assert kwargs["actions"] == ["recorded", "promoted"]
    assert kwargs["limit"] == 10
    assert kwargs["after_id"] == 5
    assert kwargs["actor"] is None


def test_changes_actor_filter_is_paired_into_an_actor(http: TestClient, client: AsyncMock) -> None:
    """`actor` is a filter, not an attested identity -- it is client-supplied
    query data, unlike the actor the write endpoints resolve via `get_actor`.
    Reuses the same `paired` helper `subject`/`evidence` use in
    `decisions.py`, just constructing an `Actor` from the pair instead of
    passing the raw tuple through."""
    client.changes.return_value = []
    http.get("/binnacle/v1/changes", params={"actor_kind": "human", "actor_id": "alice"})
    assert client.changes.await_args.kwargs["actor"] == Actor("human", "alice")


def test_half_an_actor_pair_is_rejected(http: TestClient, client: AsyncMock) -> None:
    """Half a pair would otherwise be silently dropped, widening the filter
    to "any actor" instead of failing loudly."""
    response = http.get("/binnacle/v1/changes", params={"actor_id": "alice"})
    assert response.status_code == 422
    client.changes.assert_not_awaited()


def test_precedent_requires_a_question(http: TestClient, client: AsyncMock) -> None:
    assert http.get("/binnacle/v1/precedent").status_code == 422
    client.precedent.assert_not_awaited()


def test_precedent_forwards_filters_and_serializes_hits(
    http: TestClient, client: AsyncMock
) -> None:
    decision = _compact_decision(uuid4())
    client.precedent.return_value = [PrecedentHit(decision=decision, similarity=0.87)]

    response = http.get(
        "/binnacle/v1/precedent",
        params={
            "question": "retry policy?",
            "domains": ["architecture"],
            "tiers": ["long_term"],
            "limit": 5,
            "include_dead": False,
        },
    )

    assert response.status_code == 200
    args, kwargs = client.precedent.await_args
    assert args[0] == "retry policy?"
    assert kwargs["domains"] == ["architecture"]
    assert kwargs["tiers"] == ["long_term"]
    assert kwargs["limit"] == 5
    assert kwargs["include_dead"] is False

    body = response.json()
    assert len(body) == 1
    assert body[0]["similarity"] == 0.87
    assert body[0]["decision"]["id"] == str(decision.id)
    assert body[0]["decision"]["domain"] == "architecture"
    assert body[0]["decision"]["tier"] == "long_term"
    assert body[0]["decision"]["status"] == "current"
    assert body[0]["decision"]["outcome_truncated"] == "Use PostgreSQL for the decision store."
    assert body[0]["decision"]["subject_refs"] == [
        {"role": "subject", "kind": "component", "identifier": "portolan-ingest", "note": None}
    ]


def test_precedent_defaults_are_not_forced_by_the_router(
    http: TestClient, client: AsyncMock
) -> None:
    """Unsupplied optional filters must reach the client as core's own
    defaults imply (`domains`/`tiers` absent, `limit=10`, `include_dead=True`),
    not a router-invented value."""
    client.precedent.return_value = []
    http.get("/binnacle/v1/precedent", params={"question": "retry policy?"})
    kwargs = client.precedent.await_args.kwargs
    assert kwargs["domains"] is None
    assert kwargs["tiers"] is None
    assert kwargs["limit"] == 10
    assert kwargs["include_dead"] is True


def test_export_is_a_single_response(http: TestClient, client: AsyncMock) -> None:
    """Measured at 28.5 MB / 0.61s at design scale, so one response is
    adequate; streaming would need a generator API in core."""
    export_body = {
        "schema_version": 1,
        "decisions": [
            {
                "decision_id": str(uuid4()),
                "domain": "architecture",
                "tier": "long_term",
                "status": "current",
                "scenario": "s",
                "outcome": "o",
                "reasoning": "r",
                "source": "meridian",
                "recorded_by": "human:alice",
                "recorded_at": "2026-09-01T00:00:00+00:00",
                "decided_at": None,
                "options_considered": [],
                "consequences": None,
                "confidence": None,
                "valid_from": None,
                "valid_until": None,
                "refs": [],
                "supersedes": [],
                "supplements": [],
                "metadata": {},
                "schema_version": 1,
            }
        ],
        "links": [],
        "transitions": [],
        "domains": [{"name": "architecture", "description": "d", "active": True}],
    }
    client.export.return_value = export_body

    response = http.get("/binnacle/v1/export", params={"domains": ["architecture"]})

    assert response.status_code == 200
    assert response.json() == export_body
    assert client.export.await_args.kwargs["domains"] == ["architecture"]


def test_export_forwards_tier_and_status(http: TestClient, client: AsyncMock) -> None:
    client.export.return_value = {
        "schema_version": 1,
        "decisions": [],
        "links": [],
        "transitions": [],
        "domains": [],
    }
    http.get(
        "/binnacle/v1/export",
        params={"tier": "long_term", "status": ["current", "archived"]},
    )
    kwargs = client.export.await_args.kwargs
    assert kwargs["tier"] == "long_term"
    assert kwargs["status"] == ["current", "archived"]


def test_export_defaults_are_not_forced_by_the_router(http: TestClient, client: AsyncMock) -> None:
    client.export.return_value = {
        "schema_version": 1,
        "decisions": [],
        "links": [],
        "transitions": [],
        "domains": [],
    }
    http.get("/binnacle/v1/export")
    kwargs = client.export.await_args.kwargs
    assert kwargs["domains"] is None
    assert kwargs["tier"] is None
    assert kwargs["status"] is None
