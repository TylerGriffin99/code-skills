import asyncio
from collections.abc import Awaitable, Callable, Iterable


async def bounded_gather[T, R](items: Iterable[T], fn: Callable[[T], Awaitable[R]], *, limit: int) -> list[R]:
    """Run fn over items with at most `limit` in flight; results keep input order.

    A failure cancels the remaining tasks and raises an ExceptionGroup (catch with `except*`).
    """
    if limit < 1:
        raise ValueError(f"limit must be >= 1, got {limit}")
    semaphore = asyncio.Semaphore(limit)

    async def run(item: T) -> R:
        async with semaphore:
            return await fn(item)

    async with asyncio.TaskGroup() as group:
        tasks = [group.create_task(run(item)) for item in items]
    return [task.result() for task in tasks]
