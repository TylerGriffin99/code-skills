"""Composition root: the one place long-lived resources are built, shared and closed.

Scripts call `open_resources(get_settings())`; the FastAPI lifespan calls the same function.
"""

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import Settings
from app.database.session import build_engine, build_session_factory


class Resources:
    def __init__(self, *, engine: AsyncEngine, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.engine = engine
        self.session_factory = session_factory


@asynccontextmanager
async def open_resources(settings: Settings) -> AsyncIterator[Resources]:
    async with AsyncExitStack() as stack:
        engine = build_engine(settings.database_url)
        stack.push_async_callback(engine.dispose)
        yield Resources(engine=engine, session_factory=build_session_factory(engine))
