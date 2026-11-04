import hashlib
import json
import math
from datetime import datetime

from app.modules.data_pipeline.normalizers.parking_attributes import (
    CITYDATA_PARKING_SOURCE as CITYDATA_PARKING_SOURCE,
)
from app.modules.data_pipeline.normalizers.parking_attributes import (
    MUNICIPAL_PARKING_SOURCE as MUNICIPAL_PARKING_SOURCE,
)
from app.modules.data_pipeline.normalizers.parking_attributes import (
    SOURCE as SOURCE,
)
from app.modules.data_pipeline.normalizers.parking_attributes import (
    normalize_attributes,
    text_value,
)


def attach_municipal_positions(rows, positions):
    """Use registration coordinates only when a provider ID identifies one position."""
    enriched = []
    for row in rows:
        record = dict(row)
        candidates = positions.get(str(row.get("PKLT_CD")), set())
        if len(candidates) == 1:
            record["LAT"], record["LOT"] = next(iter(candidates))
        enriched.append(record)
    return enriched


def _first_text(row: dict, *keys: str) -> str | None:
    for key in keys:
        value = text_value(row.get(key))
        if value is not None:
            return value
    return None


def normalize_parking(row: dict, collected_at: datetime, source: str = SOURCE) -> dict:
    if source == CITYDATA_PARKING_SOURCE:
        code, name = _first_text(row, "PRK_CD"), _first_text(row, "PRK_NM")
        lon_value = _first_text(row, "LNG", "LON")
        address = _first_text(row, "ROAD_ADDR", "ADDRESS")
    elif source in {SOURCE, MUNICIPAL_PARKING_SOURCE}:
        code, name = _first_text(row, "PKLT_CD"), _first_text(row, "PKLT_NM")
        lon_value, address = _first_text(row, "LOT"), _first_text(row, "ADDR")
    else:
        raise ValueError("unsupported_parking_source")
    result = {
        **parking_identity(source, code, name, address, row.get("LAT"), lon_value),
        **normalize_attributes(row, collected_at, source),
        "collected_at": collected_at,
    }
    if source == CITYDATA_PARKING_SOURCE:
        result["input_fields"] = {
            key: row[key]
            for key in (
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
            if key in row
        }
    return result


def parking_identity(source, code, name, address, lat_value, lon_value):
    """Common source-local identity/position validation, without source field aliases."""
    if not code or not name:
        raise ValueError("missing_identity")
    try:
        lat, lon = float(lat_value), float(lon_value)
    except ValueError, TypeError, KeyError:
        raise ValueError("invalid_coordinates") from None
    # Broad Seoul envelope; exact administrative containment is a separate layer.
    if (
        not math.isfinite(lat)
        or not math.isfinite(lon)
        or not (37.4 <= lat <= 37.75 and 126.7 <= lon <= 127.25)
    ):
        raise ValueError("invalid_coordinates")
    identity = json.dumps([source, code, name, address, lat, lon], ensure_ascii=False)
    return {
        # Lookup hint for existing migration records; public UUIDs are owned by storage.
        "identity_key": hashlib.sha256(identity.encode()).hexdigest(),
        "source": source,
        "source_id": code,
        "name": name,
        "address": address,
        "location": f"SRID=4326;POINT({lon} {lat})",
        "position": (lon, lat),
    }
