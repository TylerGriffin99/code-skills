"""Providers: build services and repos per request from the shared Resources. No other module constructs them."""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.bootstrap import Resources
from app.database.session import session_scope


def get_resources(request: Request) -> Resources:
    resources: Resources = request.app.state.resources
    return resources


ResourcesDep = Annotated[Resources, Depends(get_resources)]


async def get_session(resources: ResourcesDep) -> AsyncIterator[AsyncSession]:
    async with session_scope(resources.session_factory) as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]
