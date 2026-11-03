import asyncio
from collections.abc import Awaitable, Callable

from pydantic import Field

from app.schemas.base import StrictModel


class RetryPolicy(StrictModel):
    max_attempts: int = Field(default=3, ge=1)
    base_delay_seconds: float = Field(default=0.2, ge=0)
    max_delay_seconds: float = Field(default=5.0, ge=0)

    def delay_for(self, attempt: int) -> float:
        return min(self.base_delay_seconds * 2.0 ** (attempt - 1), self.max_delay_seconds)


async def retry_async[T](
    fn: Callable[[], Awaitable[T]],
    *,
    policy: RetryPolicy,
    should_retry: Callable[[Exception], bool],
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    """Call fn until it succeeds, should_retry says no, or attempts run out. The last error propagates."""
    attempt = 1
    while True:
        try:
            return await fn()
        except Exception as exc:
            if attempt >= policy.max_attempts or not should_retry(exc):
                raise
        await sleep(policy.delay_for(attempt))
        attempt += 1
