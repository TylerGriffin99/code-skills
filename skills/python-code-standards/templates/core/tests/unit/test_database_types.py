from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy.dialects import sqlite

from app.database.types import UtcDateTime

DIALECT = sqlite.dialect()


def test_bind_converts_offset_to_utc() -> None:
    plus_two = datetime(2026, 1, 1, 12, tzinfo=timezone(timedelta(hours=2)))

    assert UtcDateTime().process_bind_param(plus_two, DIALECT) == datetime(2026, 1, 1, 10, tzinfo=UTC)


def test_bind_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError, match="naive"):
        UtcDateTime().process_bind_param(datetime(2026, 1, 1), DIALECT)  # noqa: DTZ001


def test_result_attaches_utc_to_naive_value() -> None:
    assert UtcDateTime().process_result_value(datetime(2026, 1, 1), DIALECT) == datetime(2026, 1, 1, tzinfo=UTC)  # noqa: DTZ001
