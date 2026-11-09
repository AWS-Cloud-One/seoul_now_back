"""Independent hotspot overview; no candidate scoring or invented averages."""

import math
from datetime import UTC, datetime, timedelta, timezone

from sqlalchemy.exc import SQLAlchemyError

from app.core.exceptions import ServiceError
from app.services.map import information

WEATHER_FIELDS = {
    "TEMP": "temperature_celsius",
    "PM25": "pm25",
    "PM10": "pm10",
    "PRECIPITATION": "precipitation",
    "PRECPT_TYPE": "precipitation_type",
    "PCP_MSG": "precipitation_message",
    "AIR_IDX": "air_grade",
    "AIR_MSG": "air_message",
}
COMMERCE_FIELDS = {
    "AREA_CMRCL_LVL": "level",
    "AREA_SH_PAYMENT_CNT": "payment_count",
    "AREA_SH_PAYMENT_AMT_MIN": "payment_min_krw",
    "AREA_SH_PAYMENT_AMT_MAX": "payment_max_krw",
}
CATEGORY_FIELDS = {
    "RSB_LRG_CTGR": "large_category",
    "RSB_MID_CTGR": "middle_category",
    "RSB_PAYMENT_LVL": "level",
    "RSB_SH_PAYMENT_CNT": "payment_count",
    "RSB_MCT_CNT": "merchant_count",
    "RSB_MCT_TIME": "merchant_reference_month",
}


def missing(reason="NO_RECORD", *, error=False):
    return dict(
        value=None,
        status="error" if error else "unavailable",
        freshness="unknown",
        observed_at=None,
        collected_at=None,
        source=None,
        reason_code=reason,
    )


def select_values(row, fields):
    result = {}
    for original, output in fields.items():
        value = row.get(original)
        if isinstance(value, str):
            value = value.strip() or None
        if type(value) not in (str, int, float) and value is not None:
            value = None
        if isinstance(value, float) and not math.isfinite(value):
            value = None
        if (
            output
            in {
                "temperature_celsius",
                "pm25",
                "pm10",
                "payment_count",
                "merchant_count",
                "payment_min_krw",
                "payment_max_krw",
            }
            and value is not None
        ):
            try:
                value = float(value)
                if not math.isfinite(value) or (output != "temperature_celsius" and value < 0):
                    value = None
                if output in {"payment_count", "merchant_count"} and value is not None:
                    value = int(value) if value.is_integer() else None
            except ValueError, TypeError:
                value = None
        result[output] = value
    return result


def section_information(section, fields, time_key, now, max_age_seconds, *, commerce=False):
    if section is None or section == [] or section == {}:
        return missing()
    if isinstance(section, list):
        if len(section) != 1:
            return missing("PROVIDER_ERROR", error=True)
        section = section[0]
    if not isinstance(section, dict):
        return missing("PROVIDER_ERROR", error=True)
    values = select_values(section, fields)
    if commerce:
        categories = []

        def walk(value, depth=0):
            if depth > 4:
                return
            if isinstance(value, list):
                for child in value:
                    walk(child, depth + 1)
            elif isinstance(value, dict):
                if "RSB_LRG_CTGR" in value or "RSB_MID_CTGR" in value:
                    selected = select_values(value, CATEGORY_FIELDS)
                    if any(item is not None for item in selected.values()):
                        categories.append(selected)
                for child in value.values():
                    if isinstance(child, (dict, list)):
                        walk(child, depth + 1)

        walk(section)
        values["categories"] = categories
    if not any(value is not None and value != [] for value in values.values()):
        return missing()
    stamp = section.get(time_key)
    values["provider_time_text"] = stamp if isinstance(stamp, str) else None
    observed = None
    if isinstance(stamp, str):
        try:
            observed = datetime.fromisoformat(stamp)
            if observed.tzinfo is None:
                observed = observed.replace(tzinfo=timezone(timedelta(hours=9)))
            observed = observed.astimezone(UTC)
            if observed > now:
                observed = None
        except ValueError:
            pass
    freshness = (
        "unknown"
        if observed is None
        else ("fresh" if (now - observed).total_seconds() <= max_age_seconds else "stale")
    )
    return dict(
        value=values,
        status="available",
        freshness=freshness,
        observed_at=observed,
        collected_at=now,
        source="seoul_citydata",
        reason_code="SOURCE_TIME_UNKNOWN"
        if observed is None
        else "STALE_DATA"
        if freshness == "stale"
        else None,
    )


class AreaQueryService:
    def __init__(self, repository, extras=None, *, extras_max_age_seconds=1800):
        self.repository, self.extras = repository, extras
        self.extras_max_age_seconds = extras_max_age_seconds

    async def population_at(self, coordinates):
        """Read only published population facts. No roads, parking, extras or collection calls."""
        try:
            rows = await self.repository.populations_at(coordinates)
        except SQLAlchemyError:
            raise ServiceError("DATA_UNAVAILABLE", "인구 관측 자료를 조회할 수 없습니다.") from None
        now = datetime.now(UTC)
        return {
            "checked_at": now,
            "reason_code": "DATA_UNAVAILABLE"
            if rows is None
            else "OUTSIDE_OBSERVATION_AREA"
            if not rows
            else None,
            "areas": [
                {
                    "area_code": row["area_code"],
                    "name": row["name"],
                    "boundary_version": row["boundary_version"],
                    "population": missing(row["reason_code"])
                    if row["reason_code"]
                    else information("population", row["observation"], now),
                }
                for row in rows or []
            ],
        }

    async def overview(self, area_code, *, include_extras=False):
        area = await self.repository.area(area_code)
        if area is None:
            raise ServiceError("NOT_FOUND", "인구 관측 영역을 찾을 수 없습니다.", retryable=False)
        now = datetime.now(UTC)
        result = {
            "area_code": area_code,
            "name": area.name,
            "scope": "population_observation_area",
            "checked_at": now,
            "population": missing(),
            "roads": missing(),
            "parking": missing(),
            "weather": missing("NOT_REQUESTED"),
            "commerce": missing("NOT_REQUESTED"),
        }
        # Separate fields may fail while other valid facts remain usable.
        for layer in ("population", "roads", "parking"):
            try:
                ready, payload = await self.repository.read_section(area_code, layer)
                if not ready:
                    result[layer] = missing("DATA_UNAVAILABLE")
                elif layer == "population":
                    result[layer] = information(layer, payload, now)
                elif layer == "roads":
                    ids, rows = payload
                    result[layer] = dict(
                        value={
                            "items": [
                                {
                                    "link_id": key,
                                    "traffic": information("roads", rows.get(key), now),
                                }
                                for key in ids
                            ]
                        },
                        status="available",
                        freshness="unknown",
                        observed_at=None,
                        collected_at=None,
                        source=None,
                        reason_code=None,
                    )
                else:
                    count = payload
                    result[layer] = dict(
                        value={"registered_facility_count": count, "availability": "unsupported"},
                        status="available",
                        freshness="unknown",
                        observed_at=None,
                        collected_at=None,
                        source=None,
                        reason_code=None,
                    )
            except ServiceError as exc:
                result[layer] = missing(exc.code, error=True)
            except SQLAlchemyError:
                result[layer] = missing("DATA_UNAVAILABLE", error=True)
        if include_extras:
            try:
                if self.extras is None:
                    raise ServiceError(
                        "SERVICE_UNAVAILABLE", "상권·날씨 조회가 연결되지 않았습니다."
                    )
                selected = await self.extras.fetch(area_code, area.name)
                received = datetime.now(UTC)
                result["weather"] = section_information(
                    selected.get("WEATHER_STTS"),
                    WEATHER_FIELDS,
                    "WEATHER_TIME",
                    received,
                    self.extras_max_age_seconds,
                )
                result["commerce"] = section_information(
                    selected.get("LIVE_CMRCL_STTS"),
                    COMMERCE_FIELDS,
                    "CMRCL_TIME",
                    received,
                    self.extras_max_age_seconds,
                    commerce=True,
                )
            except ServiceError as exc:
                result["weather"] = result["commerce"] = missing(exc.code, error=True)
        return result
