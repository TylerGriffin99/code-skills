import asyncio

import pytest

from app.utils.concurrency import bounded_gather


async def test_keeps_input_order_when_completion_is_reversed() -> None:
    async def slow_first(n: int) -> int:
        await asyncio.sleep(0.01 * (3 - n))
        return n

    assert await bounded_gather([0, 1, 2], slow_first, limit=3) == [0, 1, 2]


async def test_never_exceeds_limit() -> None:
    in_flight = 0
    peak = 0

    async def track(_: int) -> None:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.001)
        in_flight -= 1

    await bounded_gather(range(10), track, limit=2)

    assert peak == 2


async def test_empty_input_returns_empty_list() -> None:
    async def identity(n: int) -> int:
        return n

    assert await bounded_gather([], identity, limit=1) == []


async def test_failure_raises_exception_group() -> None:
    async def fail(_: int) -> None:
        raise ValueError("bad item")

    with pytest.raises(ExceptionGroup):
        await bounded_gather([1], fail, limit=1)


async def test_rejects_limit_below_one() -> None:
    async def identity(n: int) -> int:
        return n

    with pytest.raises(ValueError, match="limit"):
        await bounded_gather([1], identity, limit=0)
