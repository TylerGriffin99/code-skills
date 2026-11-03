from datetime import datetime
from uuid import UUID

from sqlalchemy import Enum
from sqlalchemy.orm import Mapped, mapped_column

from app.constants import OrderStatus
from app.database.base import Base
from app.database.types import UtcDateTime


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    amount_cents: Mapped[int]
    status: Mapped[OrderStatus] = mapped_column(Enum(OrderStatus, native_enum=False, length=20))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime())
