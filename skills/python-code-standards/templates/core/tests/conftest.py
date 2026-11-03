from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.bootstrap import Resources, open_resources
from app.config import Settings
from app.database.base import Base


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")


@pytest.fixture
async def resources(settings: Settings) -> AsyncIterator[Resources]:
    async with open_resources(settings) as resources:
        async with resources.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        yield resources


@pytest.fixture
async def session(resources: Resources) -> AsyncIterator[AsyncSession]:
    async with resources.session_factory() as session:
        yield session
