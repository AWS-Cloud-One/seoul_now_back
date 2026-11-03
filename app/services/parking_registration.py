"""Build API registration objects from typed storage, without DB access."""


def registered_information(record):
    fees = {
        k: float(getattr(record, k)) if getattr(record, k) is not None else None
        for k in (
            "base_minutes",
            "base_fee_krw",
            "additional_minutes",
            "additional_fee_krw",
            "daily_max_krw",
        )
    }
    fees["notes"] = record.fee_notes
    hours = {
        "weekdays": record.weekday_hours,
        "saturday": record.saturday_hours,
        "sunday_holidays": record.sunday_holiday_hours,
        "notes": record.hours_notes,
    }
    return dict(
        value=dict(
            address=record.address,
            registered_capacity=record.registered_capacity,
            fees=fees if any(v is not None for v in fees.values()) else None,
            operating_hours=hours if any(v is not None for v in hours.values()) else None,
        ),
        status="available",
        freshness="unknown",
        observed_at=record.source_updated_at,
        collected_at=record.collected_at,
        source=record.source,
        reason_code=record.reason_code or "SOURCE_TIME_UNKNOWN",
    )
