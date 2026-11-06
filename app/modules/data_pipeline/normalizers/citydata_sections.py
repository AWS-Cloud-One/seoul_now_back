"""Versioned CITYDATA field selection and section presence, without SQL or I/O."""

from collections import Counter

from app.clients.seoul import seoul_result_error
from app.modules.data_pipeline.normalizers.citydata import CityDataError, validate_area
from app.modules.data_pipeline.normalizers.parking import CITYDATA_PARKING_SOURCE, normalize_parking
from app.modules.data_pipeline.normalizers.road_geometry import citydata_line

CONTRACT = "citydata-selected-v1"
POPULATION_FIELDS = (
    "AREA_CD",
    "AREA_NM",
    "AREA_PPLTN_MIN",
    "AREA_PPLTN_MAX",
    "AREA_CONGEST_LVL",
    "REPLACE_YN",
    "PPLTN_TIME",
)
PARKING_FIELDS = (
    "PRK_CD",
    "PRK_NM",
    "ROAD_ADDR",
    "ADDRESS",
    "LAT",
    "LNG",
    "LON",
    "CPCTY",
    "PRK_TYPE",
    "PAY_YN",
    "TIME_RATES",
    "RATES",
    "ADD_TIME_RATES",
    "ADD_RATES",
    "CUR_PRK_YN",
    "CUR_PRK_CNT",
    "CUR_PRK_TIME",
)
ROAD_FIELDS = ("LINK_ID", "XYLIST")
KNOWN_IGNORED = {
    "ACDNT_CNTRL_STTS",
    "SUB_STTS",
    "BUS_STN_STTS",
    "LIVE_SUB_PPLTN",
    "LIVE_BUS_PPLTN",
    "SBIKE_STTS",
    "CHARGER_STTS",
    "WEATHER_STTS",
    "EVENT_STTS",
    "CULTURALEVENTINFO",
    "LIVE_CMRCL_STTS",
    "LIVE_DST_MESSAGE",
    "LIVE_YNA_NEWS",
}
SECTION_KEYS = {
    "population": "LIVE_PPLTN_STTS",
    "parking": "PRK_STTS",
    "road_geometry": "ROAD_TRAFFIC_STTS",
}


def unwrap_citydata(body, *, expected_code=None, expected_name=None, require_envelope=False):
    """Validate provider identity and envelope before selecting any fields."""
    if require_envelope and (not isinstance(body, dict) or "RESULT" not in body):
        raise CityDataError("provider_invalid_response")
    if isinstance(body, dict) and ("CITYDATA" in body or "RESULT" in body):
        result = body.get("RESULT")
        if not isinstance(result, dict):
            raise CityDataError("provider_invalid_response")
        codes = [result[k] for k in ("CODE", "RESULT.CODE") if k in result]
        if not codes or any(not isinstance(c, str) or not c.strip() for c in codes):
            raise CityDataError("provider_invalid_response")
        if len({c.strip() for c in codes}) != 1:
            raise CityDataError("provider_invalid_response")
        if codes[0].strip() != "INFO-000":
            raise CityDataError(seoul_result_error(codes[0].strip()))
        city = body.get("CITYDATA")
    else:
        city = body
    validate_area(city, expected_code=expected_code, expected_name=expected_name)
    return city


def section_state(city, section):
    key = SECTION_KEYS[section]
    if key not in city:
        return {"state": "missing", "rows": 0}
    rows = city[key]
    if section == "road_geometry":
        if not isinstance(rows, dict):
            return {"state": "invalid", "rows": 0}
        if key not in rows:
            return {"state": "missing", "rows": 0}
        rows = rows[key]
    if section == "population" and isinstance(rows, dict):
        rows = [rows] if rows else []
    if not isinstance(rows, list):
        return {"state": "invalid", "rows": 0}
    if not rows:
        return {"state": "empty", "rows": 0}
    if all(not isinstance(r, dict) or not r for r in rows):
        return {"state": "invalid", "rows": len(rows)}
    return {"state": "usable", "rows": len(rows)}


def selected_rows(value, fields):
    if isinstance(value, list):
        return [selected_rows(row, fields) for row in value]
    if isinstance(value, dict):
        return {k: value[k] for k in fields if k in value}
    # Preserve malformed shape for rejection, without copying arbitrary unused content.
    return None


def select_citydata(city, received_at=None, *, include_road_geometry=True):
    selected = {k: city[k] for k in ("AREA_CD", "AREA_NM")}
    sections = {
        name: section_state(city, name)
        for name in SECTION_KEYS
        if include_road_geometry or name != "road_geometry"
    }
    for key, fields in (("LIVE_PPLTN_STTS", POPULATION_FIELDS), ("PRK_STTS", PARKING_FIELDS)):
        if key in city:
            selected[key] = selected_rows(city[key], fields)
    if include_road_geometry and "ROAD_TRAFFIC_STTS" in city:
        road = city["ROAD_TRAFFIC_STTS"]
        selected["ROAD_TRAFFIC_STTS"] = (
            {"ROAD_TRAFFIC_STTS": selected_rows(road["ROAD_TRAFFIC_STTS"], ROAD_FIELDS)}
            if isinstance(road, dict) and "ROAD_TRAFFIC_STTS" in road
            else {}
            if isinstance(road, dict)
            else None
        )
        if isinstance(road, dict) and "AVG_ROAD_DATA" in road:
            selected["ROAD_TRAFFIC_STTS"]["AVG_ROAD_DATA"] = selected_rows(
                road["AVG_ROAD_DATA"], ("ROAD_TRAFFIC_TIME",)
            )
    for name in ("parking", "road_geometry"):
        if name not in sections:
            continue
        section = sections[name]
        if section["state"] != "usable":
            continue
        rows = (
            selected["PRK_STTS"]
            if name == "parking"
            else selected["ROAD_TRAFFIC_STTS"]["ROAD_TRAFFIC_STTS"]
        )
        reasons = Counter()
        for row in rows:
            if name == "parking":
                try:
                    normalize_parking(row, received_at, CITYDATA_PARKING_SOURCE)
                except ValueError, TypeError, KeyError, AttributeError:
                    reasons["invalid_parking_row"] += 1
            else:
                link = row.get("LINK_ID") if isinstance(row, dict) else None
                error = (
                    citydata_line(row)[1]
                    if isinstance(link, str) and link.strip()
                    else "missing_link_id"
                )
                if error:
                    reasons[error] += 1
        section["row_validation"] = {
            "unit": "row",
            "scope": "candidate_validation",
            "received": len(rows),
            "accepted": len(rows) - sum(reasons.values()),
            "rejected": sum(reasons.values()),
            "rejection_reasons": dict(reasons),
        }
        if sum(reasons.values()) == len(rows):
            section["state"] = "invalid"
    dropped = {}
    for key, fields in (("LIVE_PPLTN_STTS", POPULATION_FIELDS), ("PRK_STTS", PARKING_FIELDS)):
        rows = city.get(key)
        rows = [rows] if isinstance(rows, dict) else rows if isinstance(rows, list) else []
        dropped[key] = sorted(
            {k for row in rows if isinstance(row, dict) for k in row} - set(fields)
        )
    known = set(SECTION_KEYS.values()) | {"AREA_CD", "AREA_NM"} | KNOWN_IGNORED
    return {
        "contract": CONTRACT,
        "CITYDATA": selected,
        "sections": sections,
        "section_state_scope": (
            "shape and candidate validation; duplicate resolution and writes are separate"
        ),
        "source_shapes": {k: type(v).__name__ for k, v in city.items()},
        "unknown_keys": sorted(set(city) - known),
        "dropped_field_names": dropped,
        "ignored_keys": sorted(set(city) & KNOWN_IGNORED),
    }
