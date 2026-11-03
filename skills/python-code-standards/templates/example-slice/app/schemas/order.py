from datetime import datetime
from uuid import UUID

from app.constants import OrderStatus
from app.schemas.base import StrictModel
from app.schemas.common import Cents


class CreateOrderRequest(StrictModel):
    amount_cents: Cents


class OrderResponse(StrictModel):
    id: UUID
    amount_cents: int
    status: OrderStatus
    created_at: datetime
