from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.order import Order


class OrderRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, order: Order) -> None:
        self.session.add(order)
        await self.session.flush()

    async def get(self, order_id: UUID) -> Order | None:
        return await self.session.get(Order, order_id)
