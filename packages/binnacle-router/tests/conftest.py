from collections.abc import AsyncIterator
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from binnacle_core import Actor, Binnacle
from binnacle_router import install_error_handlers, make_router

HUMAN = Actor("human", "alice")
AGENT = Actor("agent", "meridian/sess-1")


@pytest.fixture()
def human_actor() -> Actor:
    """Shared human actor fixture. See conftest module note below on why
    this is a fixture rather than a `from tests.conftest import HUMAN`."""
    return HUMAN


@pytest.fixture()
def agent_actor() -> Actor:
    return AGENT


@pytest.fixture()
def client() -> AsyncMock:
    """A stand-in for Binnacle. `spec=` means a typo in a method name fails
    the test rather than silently returning another mock."""
    return AsyncMock(spec=Binnacle)


@pytest.fixture()
def app(client: AsyncMock, human_actor: Actor) -> FastAPI:
    async def get_actor() -> Actor:
        return human_actor

    application = FastAPI()
    install_error_handlers(application)
    application.include_router(make_router(binnacle=client, get_actor=get_actor))
    return application


@pytest.fixture()
def http(app: FastAPI) -> AsyncIterator[TestClient]:
    with TestClient(app) as c:
        yield c
