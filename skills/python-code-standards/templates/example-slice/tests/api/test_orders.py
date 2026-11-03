from uuid import UUID, uuid4

import httpx
from fastapi import FastAPI

from app.dependencies import get_payments
from tests.fakes.payments import FakePaymentGateway


async def test_place_then_get_order(app: FastAPI, client: httpx.AsyncClient) -> None:
    payments = FakePaymentGateway()
    app.dependency_overrides[get_payments] = lambda: payments

    created = await client.post("/api/v1/orders", json={"amount_cents": 500})
    fetched = await client.get(f"/api/v1/orders/{created.json()['id']}")

    assert created.status_code == 201
    assert created.json()["status"] == "paid"
    assert fetched.json() == created.json()
    assert payments.charges == [(UUID(created.json()["id"]), 500)]


async def test_unknown_order_is_404(client: httpx.AsyncClient) -> None:
    response = await client.get(f"/api/v1/orders/{uuid4()}")

    assert response.status_code == 404
    assert response.json() == {"detail": "Order not found"}


async def test_rejects_non_positive_amount(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/v1/orders", json={"amount_cents": 0})

    assert response.status_code == 422


async def test_old_unversioned_path_is_404(client: httpx.AsyncClient) -> None:
    response = await client.get(f"/orders/{uuid4()}")

    assert response.status_code == 404
