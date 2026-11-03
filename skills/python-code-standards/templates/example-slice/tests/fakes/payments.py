from uuid import UUID

from app.schemas.payments import ChargeResult


class FakePaymentGateway:
    """In-memory PaymentGateway: records charges, or raises `fail_with`."""

    def __init__(self, *, fail_with: Exception | None = None) -> None:
        self.fail_with = fail_with
        self.charges: list[tuple[UUID, int]] = []

    async def charge(self, *, order_id: UUID, amount_cents: int) -> ChargeResult:
        if self.fail_with is not None:
            raise self.fail_with
        self.charges.append((order_id, amount_cents))
        return ChargeResult(charge_id=f"ch_{len(self.charges)}")
