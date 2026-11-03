from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import error_handlers
from app.bootstrap import open_resources
from app.config import Settings, get_settings
from app.routes import build_api_router
from app.routes.health import router as health_router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    async with open_resources(settings) as resources:
        app.state.resources = resources
        yield


def create_app(settings: Settings) -> FastAPI:
    app = FastAPI(lifespan=lifespan)
    app.state.settings = settings
    app.include_router(health_router)
    app.include_router(build_api_router(settings.api_prefix))
    error_handlers.register(app)
    return app


def build_app() -> FastAPI:
    """Entry point: `uvicorn --factory app.main:build_app`."""
    return create_app(get_settings())
