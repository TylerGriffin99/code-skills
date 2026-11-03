from functools import partial
from typing import Final, Protocol
from uuid import UUID

import httpx

from app.config import Settings
from app.schemas.payments import ChargeResult
from app.utils.retry import RetryPolicy, retry_async

RETRYABLE_STATUSES: Final = frozenset({429, 502, 503, 504})
RETRYABLE_TRANSPORT_ERRORS: Final = (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout)


class PaymentGateway(Protocol):
    async def charge(self, *, order_id: UUID, amount_cents: int) -> ChargeResult: ...


def is_retryable(exc: Exception) -> bool:
    """Transient: rate limit, gateway errors, network. Everything else (4xx auth/validation, bad URL) is fatal."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in RETRYABLE_STATUSES
    return isinstance(exc, RETRYABLE_TRANSPORT_ERRORS)


class PaymentsClient:
    def __init__(self, *, http: httpx.AsyncClient, retry: RetryPolicy) -> None:
        self.http = http
        self.retry = retry

    @staticmethod
    def build_http(settings: Settings) -> httpx.AsyncClient:
        """One pooled HTTP client per upstream; the lifespan owns and closes it."""
        return httpx.AsyncClient(
            base_url=settings.payments_url,
            headers={"Authorization": f"Bearer {settings.payments_api_key.get_secret_value()}"},
            timeout=settings.http_timeout_seconds,
        )

    async def charge(self, *, order_id: UUID, amount_cents: int) -> ChargeResult:
        # The idempotency key makes a retried POST safe: the provider charges once per order.
        request = self.http.build_request(
            "POST",
            "/charges",
            json={"reference": str(order_id), "amount_cents": amount_cents},
            headers={"Idempotency-Key": str(order_id)},
        )
        response = await retry_async(partial(self.send, request), policy=self.retry, should_retry=is_retryable)
        return ChargeResult.model_validate(response.json())

    async def send(self, request: httpx.Request) -> httpx.Response:
        response = await self.http.send(request)
        response.raise_for_status()
        return response
