"""Engine sweeps: host-scheduled maintenance operations, not user-initiated
ones. Every other write endpoint in this package carries an attested actor;
these three deliberately carry none -- `sweeps_router` takes no `get_actor`
parameter at all, so accepting an actor here is structurally impossible, not
merely unused. The no-actor test below goes further than checking `kwargs`:
it pins `await_args` in full (args and kwargs together) against a bare
`call()`, so a caller-supplied actor smuggled through positionally would
fail the assertion just as loudly as one smuggled through as a keyword.
"""

from unittest.mock import AsyncMock, call

from fastapi.testclient import TestClient

from binnacle_core import ArchivalSummary, BackfillSummary, DiscoverySummary


def test_backfill_embeddings_returns_the_full_summary(http: TestClient, client: AsyncMock) -> None:
    client.backfill_embeddings.return_value = BackfillSummary(embedded=42)
    response = http.post("/binnacle/v1/sweeps:backfill_embeddings", json={"batch": 100})
    assert response.status_code == 200
    assert response.json() == {"embedded": 42}


def test_backfill_embeddings_forwards_batch(http: TestClient, client: AsyncMock) -> None:
    client.backfill_embeddings.return_value = BackfillSummary(embedded=0)
    http.post("/binnacle/v1/sweeps:backfill_embeddings", json={"batch": 250})
    assert client.backfill_embeddings.await_args == call(batch=250)


def test_backfill_embeddings_batch_defaults_to_one_hundred(
    http: TestClient, client: AsyncMock
) -> None:
    """Matches `Binnacle.backfill_embeddings`'s own default so an empty body
    behaves identically to calling the client method directly."""
    client.backfill_embeddings.return_value = BackfillSummary(embedded=0)
    http.post("/binnacle/v1/sweeps:backfill_embeddings", json={})
    assert client.backfill_embeddings.await_args == call(batch=100)


def test_discover_returns_the_full_summary(http: TestClient, client: AsyncMock) -> None:
    client.discover.return_value = DiscoverySummary(
        decisions_processed=10,
        suggestions_enqueued=3,
        suggestions_deduped=1,
        suggestions_below_floor=2,
        promotions_recommended=1,
    )
    response = http.post("/binnacle/v1/sweeps:discover", json={"batch": 100})
    assert response.status_code == 200
    assert response.json() == {
        "decisions_processed": 10,
        "suggestions_enqueued": 3,
        "suggestions_deduped": 1,
        "suggestions_below_floor": 2,
        "promotions_recommended": 1,
    }


def test_batch_size_is_forwarded(http: TestClient, client: AsyncMock) -> None:
    client.discover.return_value = DiscoverySummary(
        decisions_processed=0,
        suggestions_enqueued=0,
        suggestions_deduped=0,
        suggestions_below_floor=0,
        promotions_recommended=0,
    )
    http.post("/binnacle/v1/sweeps:discover", json={"batch": 250})
    assert client.discover.await_args == call(batch=250)


def test_discover_batch_defaults_to_one_hundred(http: TestClient, client: AsyncMock) -> None:
    client.discover.return_value = DiscoverySummary(
        decisions_processed=0,
        suggestions_enqueued=0,
        suggestions_deduped=0,
        suggestions_below_floor=0,
        promotions_recommended=0,
    )
    http.post("/binnacle/v1/sweeps:discover", json={})
    assert client.discover.await_args == call(batch=100)


def test_archive_stale_returns_the_full_summary(http: TestClient, client: AsyncMock) -> None:
    client.archive_stale.return_value = ArchivalSummary(archived=7)
    response = http.post("/binnacle/v1/sweeps:archive_stale")
    assert response.status_code == 200
    assert response.json() == {"archived": 7}


def test_sweeps_take_no_actor(http: TestClient, client: AsyncMock) -> None:
    """Sweeps attribute their own transitions to engine:binnacle internally.
    Accepting an actor would imply the caller's identity is recorded, which
    would be a lie. `archive_stale()` takes no arguments at all in the real
    client, so pinning `await_args` to a bare `call()` -- rather than only
    checking `"actor" not in kwargs`, per the brief -- also catches an actor
    smuggled in positionally, which the kwargs-only check would miss."""
    client.archive_stale.return_value = ArchivalSummary(archived=0)
    http.post("/binnacle/v1/sweeps:archive_stale")
    assert client.archive_stale.await_args == call()


def test_malformed_batch_is_rejected_before_reaching_the_client(
    http: TestClient, client: AsyncMock
) -> None:
    response = http.post("/binnacle/v1/sweeps:discover", json={"batch": "not-a-number"})
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")
    client.discover.assert_not_awaited()


def test_the_three_sweep_paths_do_not_collide_with_each_other_or_anything_else(
    http: TestClient, client: AsyncMock
) -> None:
    client.backfill_embeddings.return_value = BackfillSummary(embedded=1)
    client.discover.return_value = DiscoverySummary(
        decisions_processed=1,
        suggestions_enqueued=0,
        suggestions_deduped=0,
        suggestions_below_floor=0,
        promotions_recommended=0,
    )
    client.archive_stale.return_value = ArchivalSummary(archived=1)

    assert http.post("/binnacle/v1/sweeps:backfill_embeddings", json={}).status_code == 200
    assert http.post("/binnacle/v1/sweeps:discover", json={}).status_code == 200
    assert http.post("/binnacle/v1/sweeps:archive_stale").status_code == 200

    client.backfill_embeddings.assert_awaited_once()
    client.discover.assert_awaited_once()
    client.archive_stale.assert_awaited_once()
