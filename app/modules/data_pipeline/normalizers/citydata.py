"""CITYDATA identity and population selection, without HTTP or SQL."""


class CityDataError(ValueError):
    """Bounded local error codes only; no provider messages or authenticated URLs."""


def validate_area(data, *, expected_code=None, expected_name=None):
    if not isinstance(data, dict):
        raise CityDataError("invalid_citydata_object")
    code, name = data.get("AREA_CD"), data.get("AREA_NM")
    if any(not isinstance(v, str) or not v.strip() for v in (code, name)):
        raise CityDataError("missing_area_identity")
    code, name = code.strip(), name.strip()
    if (expected_code is not None and code != expected_code) or (
        expected_name is not None and name != expected_name
    ):
        raise CityDataError("area_identity_mismatch")
    return code, name


def population_row(data):
    code, name = validate_area(data)
    if "LIVE_PPLTN_STTS" not in data:
        raise CityDataError("missing_population_section")
    rows = data["LIVE_PPLTN_STTS"]
    # Support singleton objects and arrays; never silently skip malformed rows.
    if isinstance(rows, dict):
        rows = [rows] if rows else []
    if not isinstance(rows, list) or any(not isinstance(row, dict) or not row for row in rows):
        raise CityDataError("invalid_population_section")
    if not rows:
        raise CityDataError("empty_population_section")
    for row in rows:
        try:
            validate_area(row, expected_code=code, expected_name=name)
        except CityDataError:
            raise CityDataError("population_area_mismatch") from None
    # Unselected demographics/forecasts do not decide which current observation is used.
    fields = ("AREA_PPLTN_MIN", "AREA_PPLTN_MAX", "AREA_CONGEST_LVL", "REPLACE_YN", "PPLTN_TIME")
    first = {key: rows[0].get(key) for key in fields}
    if any({key: row.get(key) for key in fields} != first for row in rows[1:]):
        raise CityDataError("conflicting_population_rows")
    return rows[0]
