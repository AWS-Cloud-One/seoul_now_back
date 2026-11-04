"""Small field parsers: missing/invalid are distinct, and zero is a valid value."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class FieldValue[T]:
    value: T | None = None
    reason: str | None = None

    @property
    def state(self):
        return (
            "missing" if self.reason == "MISSING_VALUE" else "invalid" if self.reason else "valid"
        )


def missing(value):
    return value is None or (isinstance(value, str) and not value.strip())


def number(value, *, integer=False):
    if missing(value):
        return FieldValue(reason="MISSING_VALUE")
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return FieldValue(reason="INVALID_NUMBER")
    try:
        parsed = Decimal(str(value).strip())
    except InvalidOperation, ValueError:
        return FieldValue(reason="INVALID_NUMBER")
    if not parsed.is_finite() or parsed < 0:
        return FieldValue(reason="INVALID_NUMBER")
    if integer and parsed != parsed.to_integral_value():
        return FieldValue(reason="INVALID_INTEGER")
    return FieldValue(int(parsed) if integer else parsed)


def timestamp(value, *, not_after=None):
    """Naive provider times are KST; explicit offsets are kept. No invented receipt time."""
    if not_after is not None and not_after.utcoffset() is None:
        raise ValueError("timestamp_limit_requires_timezone")
    if missing(value):
        return FieldValue(reason="MISSING_VALUE")
    if isinstance(value, str):
        value = value.strip()
        # A calendar date alone does not establish an observation time.
        if len(value) <= 10 or value[10] not in {"T", " ", "_"}:
            return FieldValue(reason="INVALID_TIMESTAMP")
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return FieldValue(reason="INVALID_TIMESTAMP")
    if not isinstance(value, datetime):
        return FieldValue(reason="INVALID_TIMESTAMP")
    parsed = (
        value if value.utcoffset() is not None else value.replace(tzinfo=ZoneInfo("Asia/Seoul"))
    )
    if not_after is not None and parsed > not_after:
        return FieldValue(reason="FUTURE_TIMESTAMP")
    return FieldValue(parsed)


def flag(value):
    """Y/N contract only; provider-specific numeric codes must be mapped explicitly."""
    if missing(value):
        return FieldValue(reason="MISSING_VALUE")
    if not isinstance(value, str) or value.strip() not in {"Y", "N"}:
        return FieldValue(reason="INVALID_FLAG")
    return FieldValue(value.strip() == "Y")
