"""The factory's contract: it mounts under /binnacle/v1, it takes the client
and the actor resolver from the host, and it exposes no application of its own."""

from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from binnacle_core import DomainRecord


def test_routes_are_mounted_under_the_binnacle_v1_namespace(
    http: TestClient, client: AsyncMock
) -> None:
    client.domains.return_value = [DomainRecord(name="architecture", description="d", active=True)]
    assert http.get("/binnacle/v1/domains").status_code == 200
    assert http.get("/v1/domains").status_code == 404, "must not answer outside its namespace"


def test_the_host_supplied_client_is_the_one_called(http: TestClient, client: AsyncMock) -> None:
    """The router must never construct its own Binnacle -- it cannot, since
    BinnacleConfig needs a live host-fulfilled embedder."""
    client.domains.return_value = []
    http.get("/binnacle/v1/domains")
    client.domains.assert_awaited_once()


def test_package_exports_no_runnable_app() -> None:
    """Shipping an app would recreate the deferred binnacle-service."""
    import binnacle_router

    assert not hasattr(binnacle_router, "app")
    assert hasattr(binnacle_router, "make_router")
