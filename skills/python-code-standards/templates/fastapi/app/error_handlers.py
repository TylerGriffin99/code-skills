import logging
from typing import Final

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DataError, IntegrityError, InterfaceError, NoResultFound, OperationalError

from app.exceptions import (
    AppError,
    BadRequestError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    UnauthorizedError,
    UnprocessableError,
    UpstreamError,
)

logger = logging.getLogger(__name__)

STATUS_BY_ERROR: Final[dict[type[Exception], int]] = {
    AppError: 500,
    BadRequestError: 400,
    UnauthorizedError: 401,
    ForbiddenError: 403,
    NotFoundError: 404,
    ConflictError: 409,
    UnprocessableError: 422,
    UpstreamError: 502,
    DataError: 400,
    NoResultFound: 404,
    OperationalError: 503,
    InterfaceError: 503,
    TimeoutError: 503,
    httpx.HTTPStatusError: 502,
}
HEADERS_BY_ERROR: Final[dict[type[Exception], dict[str, str]]] = {UnauthorizedError: {"WWW-Authenticate": "Bearer"}}
FOREIGN_KEY_OR_UNIQUE: Final = frozenset({"23503", "23505"})
INTERNAL_ERROR_DETAIL: Final = "Internal server error"


def lookup[V](table: dict[type[Exception], V], exc: Exception) -> V | None:
    return next((table[cls] for cls in type(exc).__mro__ if cls in table), None)


def driver_message(exc: Exception) -> str:
    """First line of the driver message. Never str(exc): it carries the SQL and its parameters."""
    orig = getattr(exc, "orig", None) or exc
    return str(orig).splitlines()[0] if str(orig) else type(orig).__name__


def integrity_status(exc: IntegrityError) -> int:
    sqlstate = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
    if sqlstate is None or sqlstate in FOREIGN_KEY_OR_UNIQUE:
        return 409
    return 400


def detail_for(exc: Exception) -> str:
    if isinstance(exc, AppError):
        return exc.detail
    if isinstance(exc, httpx.HTTPStatusError):
        return "Upstream service error"
    return driver_message(exc)


async def handle_known_error(_request: Request, exc: Exception) -> JSONResponse:
    status = integrity_status(exc) if isinstance(exc, IntegrityError) else lookup(STATUS_BY_ERROR, exc) or 500
    logger.warning("%s -> %d", type(exc).__name__, status)
    return JSONResponse({"detail": detail_for(exc)}, status_code=status, headers=lookup(HEADERS_BY_ERROR, exc))


async def handle_unexpected_error(_request: Request, exc: Exception) -> JSONResponse:
    logger.error("Unhandled %s", type(exc).__name__, exc_info=exc)
    return JSONResponse({"detail": INTERNAL_ERROR_DETAIL}, status_code=500)


def register(app: FastAPI) -> None:
    for error_type in (*STATUS_BY_ERROR, IntegrityError):
        app.add_exception_handler(error_type, handle_known_error)
    app.add_exception_handler(Exception, handle_unexpected_error)
