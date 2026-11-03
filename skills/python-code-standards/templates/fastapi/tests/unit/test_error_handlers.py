import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.exc import IntegrityError, OperationalError

from app import error_handlers
from app.exceptions import AppError, ConflictError, NotFoundError, UnauthorizedError

SECRET_SQL = "SELECT secret FROM users WHERE token = 'abc'"


class DriverError(Exception):
    def __init__(self, message: str, sqlstate: str | None) -> None:
        super().__init__(message)
        self.sqlstate = sqlstate


class OrderNotFoundError(NotFoundError): ...


CASES: list[tuple[Exception, int, str]] = [
    (NotFoundError("Order not found"), 404, "Order not found"),
    (OrderNotFoundError("Order 7 not found"), 404, "Order 7 not found"),
    (ConflictError("Already paid"), 409, "Already paid"),
    (AppError("Unmapped domain error"), 500, "Unmapped domain error"),
    (IntegrityError(SECRET_SQL, {}, DriverError("duplicate key\nDETAIL: x", "23505")), 409, "duplicate key"),
    (IntegrityError(SECRET_SQL, {}, DriverError("null value", "23502")), 400, "null value"),
    (OperationalError(SECRET_SQL, {}, DriverError("", None)), 503, "DriverError"),
    (RuntimeError("boom with secrets"), 500, "Internal server error"),
]


def build_app(exc: Exception) -> FastAPI:
    app = FastAPI()
    error_handlers.register(app)

    @app.get("/raise")
    async def raise_it() -> None:
        raise exc

    return app


@pytest.mark.parametrize(("exc", "status", "detail"), CASES)
async def test_error_maps_to_status_and_safe_detail(exc: Exception, status: int, detail: str) -> None:
    transport = httpx.ASGITransport(app=build_app(exc), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/raise")

    assert response.status_code == status
    assert response.json() == {"detail": detail}
    assert "SELECT" not in response.text
    assert "DETAIL" not in response.text


async def test_unauthorized_sends_www_authenticate() -> None:
    transport = httpx.ASGITransport(app=build_app(UnauthorizedError("Bad token")), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/raise")

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
