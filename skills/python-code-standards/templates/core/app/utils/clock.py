from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID, uuid4

type Clock = Callable[[], datetime]
type IdFactory = Callable[[], UUID]


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_id() -> UUID:
    return uuid4()
