import pytest

from app.exceptions import NotFoundError, require


def test_require_returns_the_value_unchanged() -> None:
    value = object()

    assert require(value, "Thing") is value


def test_require_keeps_falsy_values() -> None:
    assert require([], "Thing") == []


def test_require_raises_not_found_for_none() -> None:
    with pytest.raises(NotFoundError, match=r"^Thing not found$"):
        require(None, "Thing")
