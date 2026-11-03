from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.payments import PaymentGateway
from app.constants import OrderStatus
from app.exceptions import require
from app.models.order import Order
from app.repos.order_repo import OrderRepo
from app.schemas.order import CreateOrderRequest, OrderResponse
from app.utils.clock import Clock, IdFactory


class OrderService:
    def __init__(
        self, *, session: AsyncSession, orders: OrderRepo, payments: PaymentGateway, clock: Clock, new_id: IdFactory
    ) -> None:
        self.session = session
        self.orders = orders
        self.payments = payments
        self.clock = clock
        self.new_id = new_id

    async def place_order(self, request: CreateOrderRequest) -> OrderResponse:
        order = Order(
            id=self.new_id(), amount_cents=request.amount_cents, status=OrderStatus.PENDING, created_at=self.clock()
        )
        await self.orders.add(order)
        await self.payments.charge(order_id=order.id, amount_cents=order.amount_cents)
        order.status = OrderStatus.PAID
        await self.session.commit()
        return OrderResponse.model_validate(order, from_attributes=True)

    async def get_order(self, order_id: UUID) -> OrderResponse:
        order = require(await self.orders.get(order_id), "Order")
        return OrderResponse.model_validate(order, from_attributes=True)
