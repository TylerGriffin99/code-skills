from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import OrderStatus
from app.exceptions import NotFoundError, UpstreamError
from app.repos.order_repo import OrderRepo
from app.schemas.order import CreateOrderRequest
from app.services.order_service import OrderService
from tests.fakes.payments import FakePaymentGateway

NOW = datetime(2026, 1, 1, tzinfo=UTC)
ORDER_ID = UUID(int=1)


def build_service(session: AsyncSession, payments: FakePaymentGateway) -> OrderService:
    return OrderService(
        session=session, orders=OrderRepo(session), payments=payments, clock=lambda: NOW, new_id=lambda: ORDER_ID
    )


async def test_place_order_charges_once_and_marks_paid(session: AsyncSession) -> None:
    payments = FakePaymentGateway()

    order = await build_service(session, payments).place_order(CreateOrderRequest(amount_cents=500))

    assert order.id == ORDER_ID
    assert order.status is OrderStatus.PAID
    assert order.created_at == NOW
    assert payments.charges == [(ORDER_ID, 500)]


async def test_failed_charge_propagates_and_leaves_order_pending(session: AsyncSession) -> None:
    service = build_service(session, FakePaymentGateway(fail_with=UpstreamError("Payment provider unavailable")))

    with pytest.raises(UpstreamError):
        await service.place_order(CreateOrderRequest(amount_cents=500))
    await session.rollback()

    assert (await service.get_order(ORDER_ID)).status is OrderStatus.PENDING


async def test_unknown_order_raises_not_found(session: AsyncSession) -> None:
    with pytest.raises(NotFoundError, match="Order not found"):
        await build_service(session, FakePaymentGateway()).get_order(ORDER_ID)
