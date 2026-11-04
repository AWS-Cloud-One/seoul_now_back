"""Pure field conversions for the normalized storage contract."""

from app.modules.data_pipeline.normalizers.citydata import population_row
from app.modules.data_pipeline.normalizers.values import (
    FieldValue,
    flag,
    missing,
    number,
    timestamp,
)

CONGESTION = {
    "여유": "relaxed",
    "보통": "normal",
    "약간 붐빔": "slightly_crowded",
    "붐빔": "crowded",
}


def text_value(value):
    return str(value).strip() or None if value is not None else None


def population(data, *, received_at=None):
    row = population_row(data)
    parsed = {
        "min_people": number(row.get("AREA_PPLTN_MIN"), integer=True),
        "max_people": number(row.get("AREA_PPLTN_MAX"), integer=True),
        "observed_at": timestamp(row.get("PPLTN_TIME"), not_after=received_at),
        "is_replacement": flag(row.get("REPLACE_YN")),
    }
    grade = row.get("AREA_CONGEST_LVL")
    parsed["congestion"] = (
        FieldValue(reason="MISSING_VALUE")
        if missing(grade)
        else FieldValue(CONGESTION[grade.strip()])
        if isinstance(grade, str) and grade.strip() in CONGESTION
        else FieldValue(reason="UNKNOWN_CONGESTION")
    )
    for key in ("min_people", "max_people"):
        if parsed[key].value is not None and parsed[key].value > 2147483647:
            parsed[key] = FieldValue(reason="INTEGER_OUT_OF_RANGE")
    low, high = parsed["min_people"].value, parsed["max_people"].value
    if low is not None and high is not None and low > high:
        parsed["min_people"] = parsed["max_people"] = FieldValue(reason="REVERSED_RANGE")
    issues = {k: v.reason for k, v in parsed.items() if v.reason}
    observed = parsed["observed_at"]
    error = (
        "future_population_time"
        if observed.reason == "FUTURE_TIMESTAMP"
        else "invalid_population_time"
        if observed.state == "invalid"
        else "invalid_or_missing_population_range"
        if all(parsed[k].value is None for k in ("min_people", "max_people"))
        else None
    )
    return {
        **{k: v.value for k, v in parsed.items()},
        "reason_code": "SOURCE_TIME_UNKNOWN"
        if observed.state == "missing"
        else "PARTIAL_POPULATION_VALUES"
        if issues
        else None,
        "quality": {
            "section_status": "invalid" if error else "partial" if issues else "usable",
            "field_states": {k: v.state for k, v in parsed.items()},
            "field_issues": issues,
            "range_complete": all(
                parsed[k].value is not None for k in ("min_people", "max_people")
            ),
            "error_code": error,
        },
    }


def parking_fields(record):
    """Scalar registration facts; repeating hours/issues and live state have own tables."""
    return dict(
        provider_record_id=record["source_id"],
        name=record["name"],
        address=record.get("address"),
        registered_capacity=record["registered_capacity"],
        **(record["fees"] or {}),
        **record["conditions"],
        **({"fee_notes": None, "hours_notes": None} if record["fees"] is not None else {}),
        source_updated_at=record["source_updated_at"],
        reason_code="SOURCE_TIME_UNKNOWN" if record["source_updated_at"] is None else None,
    )


def road_link_fields(feature, version):
    properties = feature.get("properties") or {}
    return dict(
        link_id=str(feature["id"]),
        name=properties.get("name"),
        direction=properties.get("direction"),
        geometry=feature["geometry"],
        source_version=version,
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
    """TOPIS has no observation timestamp; classify values independently of that limit."""
    parsed = {
        "speed_kmh": number(row.get("prcs_spd")),
        "travel_seconds": number(row.get("prcs_trv_time")),
    }
    valid = sum(item.state == "valid" for item in parsed.values())
    state = (
        "valid"
        if valid == 2
        else "partial"
        if valid
        else "missing"
        if all(item.state == "missing" for item in parsed.values())
        else "invalid"
    )
    return dict(
        **{key: item.value for key, item in parsed.items()},
        observed_at=None,
        reason_code="SOURCE_TIME_UNKNOWN"
        if state == "valid"
        else "PARTIAL_TRAFFIC_VALUES"
        if state == "partial"
        else "MISSING_TRAFFIC_VALUES"
        if state == "missing"
        else "INVALID_TRAFFIC_VALUES",
        value_status=state,
        field_issues={key: item.reason for key, item in parsed.items() if item.reason},
        value_counts={
            "valid": valid,
            "missing": sum(v.state == "missing" for v in parsed.values()),
            "invalid": sum(v.state == "invalid" for v in parsed.values()),
        },
    )
