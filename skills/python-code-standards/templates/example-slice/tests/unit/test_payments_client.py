import json
from uuid import UUID

import httpx
import pytest

from app.clients.payments import PaymentsClient
from app.utils.retry import RetryPolicy

ORDER_ID = UUID(int=7)
NO_DELAY = RetryPolicy(max_attempts=3, base_delay_seconds=0)


def build_client(statuses: list[int], seen: list[httpx.Request]) -> PaymentsClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        status = statuses[len(seen) - 1]
        return httpx.Response(status, json={"charge_id": "ch_1", "extra": "ignored"})

    http = httpx.AsyncClient(base_url="https://payments.test", transport=httpx.MockTransport(handler))
    return PaymentsClient(http=http, retry=NO_DELAY)


async def test_retries_transient_status_with_the_same_idempotency_key() -> None:
    seen: list[httpx.Request] = []

    result = await build_client([503, 200], seen).charge(order_id=ORDER_ID, amount_cents=500)

    assert result.charge_id == "ch_1"
    assert len(seen) == 2
    assert {r.headers["Idempotency-Key"] for r in seen} == {str(ORDER_ID)}
    assert json.loads(seen[0].content) == {"reference": str(ORDER_ID), "amount_cents": 500}


@pytest.mark.parametrize("status", [400, 401, 402, 404])
async def test_fatal_status_makes_exactly_one_call(status: int) -> None:
    seen: list[httpx.Request] = []

    with pytest.raises(httpx.HTTPStatusError):
        await build_client([status], seen).charge(order_id=ORDER_ID, amount_cents=500)
    assert len(seen) == 1


async def test_gives_up_after_max_attempts() -> None:
    seen: list[httpx.Request] = []

    with pytest.raises(httpx.HTTPStatusError):
        await build_client([503, 503, 503], seen).charge(order_id=ORDER_ID, amount_cents=500)
    assert len(seen) == NO_DELAY.max_attempts
