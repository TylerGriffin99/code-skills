from enum import StrEnum
from typing import Final

MAX_ORDER_CENTS: Final = 1_000_000


class OrderStatus(StrEnum):
    PENDING = "pending"
    PAID = "paid"
