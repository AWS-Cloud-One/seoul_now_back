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
    hours = operating_hours(record)
    # One legacy response timestamp cannot describe independently received method facts.
    collected = record.collected_at
    if record.operation_processed_at is not None and record.operation_received_at != collected:
        collected = None
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
        collected_at=collected,
        source=record.source,
        reason_code=record.reason_code
        or ("SOURCE_TIME_UNKNOWN" if record.source_updated_at is None else None),
    )


def operating_hours(record):
    """Read structured periods without relabelling weekend as Saturday in the old API."""
    result = dict(weekdays=None, saturday=None, sunday_holidays=None, notes=None)
    labels = {
        "weekday": "평일",
        "weekend": "주말",
        "holiday": "공휴일",
        "monday": "월요일",
        "tuesday": "화요일",
        "wednesday": "수요일",
        "thursday": "목요일",
        "friday": "금요일",
        "saturday": "토요일",
        "sunday": "일요일",
        "sunday_holiday": "일요일/공휴일",
    }
    keys = {"weekday": "weekdays", "saturday": "saturday", "sunday_holiday": "sunday_holidays"}
    notes = []
    for period in record.operating_hours:
        value = None
        if period.state == "valid":
            end = period.closes_at.strftime("%H:%M:%S" if period.closes_at.second else "%H:%M")
            if period.end_day_offset:
                end = "24:00" if end == "00:00" else "익일 " + end
            value = (
                period.opens_at.strftime("%H:%M:%S" if period.opens_at.second else "%H:%M")
                + "~"
                + end
            )
            if key := keys.get(period.day_type):
                compact = value.replace(":", "")
                result[key] = "; ".join(filter(None, (result[key], compact)))
        notes.append(f"{labels[period.day_type]} {value or '미확인'}")
    if record.hours_notes:
        notes.append(record.hours_notes)
    result["notes"] = "; ".join(notes) or None
    return result
