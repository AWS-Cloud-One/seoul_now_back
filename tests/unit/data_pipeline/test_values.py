from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.modules.data_pipeline.normalizers.values import flag, number, timestamp
from app.modules.data_pipeline.results import ProcessingCounts


@pytest.mark.parametrize(
    "raw,value,state",
    [
        (None, None, "missing"),
        ("  ", None, "missing"),
        ("0", 0, "valid"),
        (0, 0, "valid"),
        ("1.0", 1, "valid"),
        (1.5, None, "invalid"),
        (True, None, "invalid"),
        ("NaN", None, "invalid"),
        ("Infinity", None, "invalid"),
        (-1, None, "invalid"),
        ({}, None, "invalid"),
    ],
)
def test_integer_observations_preserve_zero_and_reject_coercion(raw, value, state):
    parsed = number(raw, integer=True)
    assert (parsed.value, parsed.state) == (value, state)


def test_noninteger_measurements_keep_decimal_precision():
    assert number("12.345").value == Decimal("12.345")


@pytest.mark.parametrize(
    "raw,value,state",
    [
        ("Y", True, "valid"),
        ("N", False, "valid"),
        (None, None, "missing"),
        ("", None, "missing"),
        ("?", None, "invalid"),
        (1, None, "invalid"),
        (True, None, "invalid"),
    ],
)
def test_flags_do_not_collapse_unknown_into_not_provided(raw, value, state):
    parsed = flag(raw)
    assert (parsed.value, parsed.state) == (value, state)


def test_timestamp_timezone_fractional_seconds_and_future_boundary():
    limit = datetime(2026, 11, 4, 0, 0, 1, tzinfo=UTC)
    naive = timestamp("2026-11-04_09:00:00.5", not_after=limit)
    explicit = timestamp("2026-11-04T00:00:00.5+00:00", not_after=limit)
    assert naive.value == explicit.value
    assert explicit.value.utcoffset().total_seconds() == 0
    assert timestamp(limit, not_after=limit).state == "valid"
    assert timestamp("2026-11-04 09:00:02", not_after=limit).reason == "FUTURE_TIMESTAMP"
    assert timestamp("bad").reason == "INVALID_TIMESTAMP"
    assert timestamp(123).reason == "INVALID_TIMESTAMP"
    assert timestamp("").reason == "MISSING_VALUE"
    with pytest.raises(ValueError, match="timestamp_limit_requires_timezone"):
        timestamp("2026-11-04 09:00", not_after=datetime(2026, 11, 4))


def test_processing_counts_expose_units_and_conflicts_are_not_double_counted():
    result = ProcessingCounts(
        unit="row",
        scope="test",
        received=5,
        accepted=2,
        rejected=2,
        duplicates=1,
        conflicts=1,
        rejection_reasons={"invalid_value": 1, "conflicting_value": 1},
    )
    assert result.as_dict()["unit"] == "row"
    assert result.as_dict()["conflicts"] == 1
    assert result.database_counts()["rejected"] == 2


@pytest.mark.parametrize(
    "counts",
    [
        {"received": 1},
        {"received": 1, "accepted": 2},
        {"received": -1},
        {"received": True, "accepted": True},
        {"received": 1, "accepted": 1, "conflicts": 1},
    ],
)
def test_inconsistent_processing_counts_are_rejected(counts):
    with pytest.raises(ValueError):
        ProcessingCounts(unit="snapshot", scope="test", **counts)
