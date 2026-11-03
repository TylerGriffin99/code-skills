import pytest

from app.utils.retry import RetryPolicy, retry_async

POLICY = RetryPolicy(max_attempts=3, base_delay_seconds=0)


class Flaky:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    async def __call__(self) -> str:
        self.calls += 1
        if self.calls <= self.failures:
            raise ConnectionError("down")
        return "ok"


async def no_sleep(_seconds: float) -> None:
    return None


async def test_retries_until_success() -> None:
    fn = Flaky(failures=2)

    assert await retry_async(fn, policy=POLICY, should_retry=lambda _: True, sleep=no_sleep) == "ok"
    assert fn.calls == 3


async def test_gives_up_after_max_attempts() -> None:
    fn = Flaky(failures=5)

    with pytest.raises(ConnectionError):
        await retry_async(fn, policy=POLICY, should_retry=lambda _: True, sleep=no_sleep)
    assert fn.calls == 3


async def test_non_retryable_error_makes_one_call() -> None:
    fn = Flaky(failures=5)

    with pytest.raises(ConnectionError):
        await retry_async(fn, policy=POLICY, should_retry=lambda _: False, sleep=no_sleep)
    assert fn.calls == 1


def test_delay_grows_and_caps() -> None:
    policy = RetryPolicy(base_delay_seconds=1, max_delay_seconds=3)

    assert [policy.delay_for(n) for n in (1, 2, 3)] == [1, 2, 3]
