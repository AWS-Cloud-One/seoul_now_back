"""Pure field conversions for the normalized storage contract."""

from datetime import datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

CONGESTION = {
    "여유": "relaxed",
    "보통": "normal",
    "약간 붐빔": "slightly_crowded",
    "붐빔": "crowded",
}


def number(value, *, integer=False):
    if value is None or isinstance(value, bool):
        return None
    try:
        value = Decimal(str(value).strip())
    except InvalidOperation, ValueError:
        return None
    if not value.is_finite() or value < 0 or (integer and value != value.to_integral_value()):
        return None
    return int(value) if integer else value


def timestamp(value):
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=ZoneInfo("Asia/Seoul"))
    if not value or not isinstance(value, str):
        return None
    try:
        return timestamp(datetime.fromisoformat(value.strip()))
    except ValueError:
        return None


def text_value(value):
    return str(value).strip() or None if value is not None else None


def first(value):
    if isinstance(value, list):
        return next((row for row in value if isinstance(row, dict)), {})
    return value if isinstance(value, dict) else {}


def population(data):
    row = first(data.get("LIVE_PPLTN_STTS"))
    if not row:
        return None
    low = number(row.get("AREA_PPLTN_MIN"), integer=True)
    high = number(row.get("AREA_PPLTN_MAX"), integer=True)
    if low is not None and high is not None and low > high:
        low = high = None
    return dict(
        min_people=low,
        max_people=high,
        congestion=CONGESTION.get(row.get("AREA_CONGEST_LVL")),
        is_replacement={"Y": True, "N": False}.get(row.get("REPLACE_YN")),
        observed_at=timestamp(row.get("PPLTN_TIME")),
        reason_code=None if low is not None and high is not None else "INVALID_OR_MISSING_VALUE",
    )


def parking_fields(record):
    d = record.get("details") or {}
    getpark = record["source"] == "seoul_getparkinfo"
    keys = (
        ("PRK_HM", "PRK_CRG", "ADD_UNIT_TM_MNT", "ADD_CRG")
        if getpark
        else ("TIME_RATES", "RATES", "ADD_TIME_RATES", "ADD_RATES")
    )
    fees = dict(
        zip(
            ("base_minutes", "base_fee_krw", "additional_minutes", "additional_fee_krw"),
            (number(d.get(k)) for k in keys),
            strict=True,
        )
    )

    def hours(start, end):
        a, b = text_value(d.get(start)), text_value(d.get(end))
        return f"{a}~{b}" if a is not None and b is not None else None

    return dict(
        provider_record_id=record["source_id"],
        name=record["name"],
        address=record.get("address"),
        registered_capacity=number(
            d.get("TPKCT") if d.get("TPKCT") is not None else d.get("CPCTY"), integer=True
        ),
        **fees,
        daily_max_krw=None,
        fee_notes=None,
        weekday_hours=hours("WD_OPER_BGNG_TM", "WD_OPER_END_TM"),
        saturday_hours=hours("WE_OPER_BGNG_TM", "WE_OPER_END_TM"),
        sunday_holiday_hours=hours("LHLDY_BGNG", "LHLDY"),
        hours_notes=None,
        source_updated_at=record.get("source_updated_at") if getpark else None,
    )


def road_link_fields(feature, version):
    properties = feature.get("properties") or {}
    return dict(
        link_id=str(feature["id"]),
        name=properties.get("name"),
        direction=properties.get("direction"),
        geometry=feature["geometry"],
        source_version=str(properties.get("source_year") or version),
    )


def population_area_fields(feature):
    return dict(
        area_code=str(feature["id"]),
        name=feature["properties"]["name"],
        geometry=feature["geometry"],
    )


def administrative_area_fields(feature, version):
    properties = feature.get("properties") or {}
    geometry = feature["geometry"]
    parent = properties.get("sigungu_code")
    if not parent:
        return None
    return dict(
        dong_code=str(feature["id"]),
        name=properties["name"],
        sigungu_code=str(parent),
        code_system="sgis",
        geometry=geometry,
        boundary_version=version,
    )


def region_boundary_fields(feature, version, year):
    return dict(
        code_system="sgis",
        region_code="11",
        name="서울특별시",
        geometry=feature["geometry"],
        source_version=str(year) if year else None,
        boundary_version=version,
    )


def road_traffic_fields(row):
    return dict(
        speed_kmh=number(row.get("prcs_spd")),
        travel_seconds=number(row.get("prcs_trv_time")),
        observed_at=None,
        reason_code="SOURCE_TIME_UNKNOWN",
    )
