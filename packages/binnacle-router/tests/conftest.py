from collections.abc import AsyncIterator
from unittest.mock import create_autospec

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
def client() -> Binnacle:
    """A stand-in for Binnacle. `create_autospec(..., spec_set=True)` checks
    every call against `Binnacle`'s real signatures -- an unknown keyword
    argument or wrong arity fails the test, rather than an `AsyncMock(spec=)`
    which only guards attribute *names* (a typo'd method) and happily
    accepts any arguments to a real one."""
    return create_autospec(Binnacle, spec_set=True, instance=True)


@pytest.fixture()
def app(client: Binnacle, human_actor: Actor) -> FastAPI:
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
