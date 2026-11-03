from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI

from app.bootstrap import Resources
from app.config import Settings
from app.database.base import Base
from app.main import create_app


@pytest.fixture
async def app(settings: Settings) -> AsyncIterator[FastAPI]:
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        resources: Resources = app.state.resources
        async with resources.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        yield app


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client
