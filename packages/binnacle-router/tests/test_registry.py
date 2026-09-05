"""Domain registry management and the two dashboard aggregates. `GET
/domains` itself is exercised by `test_router_wiring.py` (it moved into this
module's router from Task 2's placeholder in `router.py`, but that move must
not change its behaviour, so it is not re-tested here).

Deviation from the brief's literal test text (declared per GUIDELINES §1.1,
matching the precedent already set in `test_decisions_write.py` and
`test_queue.py`): the actor is taken as the `human_actor` fixture parameter
rather than `from tests.conftest import ...` -- the task's own standing
ruling forbids importing from `conftest.py` in a test module. The brief's
three given tests are kept, plus coverage for `POST /domains` and
`PATCH /domains/{name}` (both actor-bearing writes, per the same actor-
identity convention `test_queue.py` and `test_decisions_write.py` follow) and
explicit route-resolution checks for the three ordering questions the task
called out.
"""

from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from binnacle_core import Actor, DomainRecord, DomainSummary


def test_domain_summary_reports_zero_decision_domains(http: TestClient, client: AsyncMock) -> None:
    """Zero-decision domains are the rows the registry-housekeeping use case
    exists to surface -- they must survive serialization, not be filtered out."""
    client.domain_summary.return_value = [
        DomainSummary(name="unused", description="nothing here", active=True, decision_count=0)
    ]
    body = http.get("/binnacle/v1/domains/summary").json()
    assert body == [
        {"name": "unused", "description": "nothing here", "active": True, "decision_count": 0}
    ]


def test_queue_summary_forwards_the_domains_filter(http: TestClient, client: AsyncMock) -> None:
    client.queue_summary.return_value = {"promote": 3, "conflict": 1}
    body = http.get("/binnacle/v1/queue/summary", params={"domains": ["architecture"]}).json()
    assert body == {"promote": 3, "conflict": 1}
    assert client.queue_summary.await_args.args[0] == ["architecture"]


def test_queue_summary_with_no_domains_filter_passes_none(
    http: TestClient, client: AsyncMock
) -> None:
    client.queue_summary.return_value = {}
    http.get("/binnacle/v1/queue/summary")
    assert client.queue_summary.await_args.args[0] is None


def test_deactivating_a_domain_is_a_verb_not_a_delete(http: TestClient, client: AsyncMock) -> None:
    """Nothing in binnacle is deleted; deactivation is a transition-logged act."""
    client.deactivate_domain.return_value = None
    assert (
        http.post(
            "/binnacle/v1/domains/legacy:deactivate", json={"reason": "superseded by architecture"}
        ).status_code
        == 200
    )
    assert http.delete("/binnacle/v1/domains/legacy").status_code == 405


def test_deactivate_domain_parses_the_name_without_the_suffix_bleeding_in(
    http: TestClient, client: AsyncMock, human_actor: Actor
) -> None:
    """`:deactivate` sits directly after the `{name}` path parameter -- this
    must resolve to the route (not 404) and hand the client a clean name, not
    `"legacy:deactivate"`."""
    client.deactivate_domain.return_value = None
    response = http.post("/binnacle/v1/domains/legacy:deactivate", json={})
    assert response.status_code == 200
    assert client.deactivate_domain.await_args.args[0] == "legacy"
    assert client.deactivate_domain.await_args.kwargs["actor"] == human_actor
    assert client.deactivate_domain.await_args.kwargs["reason"] is None


def test_deactivate_domain_forwards_the_reason(http: TestClient, client: AsyncMock) -> None:
    client.deactivate_domain.return_value = None
    http.post("/binnacle/v1/domains/legacy:deactivate", json={"reason": "superseded"})
    assert client.deactivate_domain.await_args.kwargs["reason"] == "superseded"


def test_add_domain_passes_the_resolved_actor_not_a_client_supplied_one(
    http: TestClient, client: AsyncMock, human_actor: Actor
) -> None:
    client.add_domain.return_value = None
    response = http.post(
        "/binnacle/v1/domains",
        json={"name": "architecture", "description": "system design decisions"},
        headers={"X-Actor-Kind": "human", "X-Actor-Id": "mallory"},
    )
    assert response.status_code == 200
    assert client.add_domain.await_args.args[0] == "architecture"
    assert client.add_domain.await_args.args[1] == "system design decisions"
    assert client.add_domain.await_args.kwargs["actor"] == human_actor


def test_update_domain_passes_the_name_description_and_resolved_actor(
    http: TestClient, client: AsyncMock, human_actor: Actor
) -> None:
    client.update_domain.return_value = None
    response = http.patch(
        "/binnacle/v1/domains/architecture", json={"description": "revised description"}
    )
    assert response.status_code == 200
    assert client.update_domain.await_args.args[0] == "architecture"
    assert client.update_domain.await_args.args[1] == "revised description"
    assert client.update_domain.await_args.kwargs["actor"] == human_actor


def test_domains_summary_is_not_swallowed_by_the_name_path_param_route(
    http: TestClient, client: AsyncMock
) -> None:
    """`GET /domains/summary` sits alongside `PATCH /domains/{name}` and
    `POST /domains/{name}:deactivate` -- neither of those is a `GET` route,
    so this checks the summary route resolves to `domain_summary()` rather
    than 404 or being coerced into a path-param match."""
    client.domain_summary.return_value = []
    response = http.get("/binnacle/v1/domains/summary")
    assert response.status_code == 200
    client.domain_summary.assert_awaited_once()
    client.update_domain.assert_not_awaited()
    client.deactivate_domain.assert_not_awaited()


def test_queue_summary_is_not_shadowed_by_the_queue_router(
    http: TestClient, client: AsyncMock
) -> None:
    """`GET /queue/summary` is declared by the registry router, but `/queue`
    paths are already declared by `queue_router` -- this checks it actually
    resolves to `queue_summary()`, not `queue()` or a 404."""
    client.queue_summary.return_value = {}
    response = http.get("/binnacle/v1/queue/summary")
    assert response.status_code == 200
    client.queue_summary.assert_awaited_once()
    client.queue.assert_not_awaited()


def test_the_moved_list_domains_still_resolves(http: TestClient, client: AsyncMock) -> None:
    """`GET /domains` moved out of `router.py`'s inline placeholder into this
    module -- `test_router_wiring.py` pins its status code and that the host
    client is called; this pins the actual body still serializes correctly
    from its new home."""
    client.domains.return_value = [
        DomainRecord(name="architecture", description="system design decisions", active=True)
    ]
    body = http.get("/binnacle/v1/domains").json()
    assert body == [
        {"name": "architecture", "description": "system design decisions", "active": True}
    ]
