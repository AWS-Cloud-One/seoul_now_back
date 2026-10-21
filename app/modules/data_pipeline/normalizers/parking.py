import hashlib
import json
import math
from datetime import datetime
from zoneinfo import ZoneInfo

SOURCE = "seoul_getparkinfo"
MUNICIPAL_PARKING_SOURCE = "seoul_municipal_parking"
CITYDATA_PARKING_SOURCE = "seoul_citydata_prk"


def text_value(value: object) -> str | None:
    return str(value).strip() if value is not None and str(value).strip() else None


def _first_text(row: dict, *keys: str) -> str | None:
    for key in keys:
        value = text_value(row.get(key))
        if value is not None:
            return value
    return None


def source_time(value: object) -> datetime | None:
    raw = text_value(value)
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=ZoneInfo("Asia/Seoul"))
    except ValueError:
        return None


def normalize_parking(row: dict, collected_at: datetime, source: str = SOURCE) -> dict:
    if source == MUNICIPAL_PARKING_SOURCE:
        row = dict(row)
        row["PRK_CD"], row["PRK_NM"] = row.get("PKLT_CD"), row.get("PKLT_NM")
        row["LNG"], row["ROAD_ADDR"] = row.get("LOT"), row.get("ADDR")
        row["CPCTY"] = row.get("TPKCT")
        row["CUR_PRK_TIME"] = row.get("NOW_PRK_VHCL_UPDT_TM")
        row["CUR_PRK_YN"] = "Y" if str(row.get("PRK_STTS_YN")) in {"1", "Y"} else "N"
        try:
            capacity_value = float(row.get("TPKCT"))
            occupied_value = float(row.get("NOW_PRK_VHCL_CNT"))
            if not all(
                math.isfinite(v) and v.is_integer() for v in (capacity_value, occupied_value)
            ):
                raise ValueError("invalid_occupancy")
            capacity = int(capacity_value)
            occupied = int(occupied_value)
            row["CUR_PRK_CNT"] = capacity - occupied if 0 <= occupied <= capacity else None
        except TypeError, ValueError:
            row["CUR_PRK_CNT"] = None
    if source in {CITYDATA_PARKING_SOURCE, MUNICIPAL_PARKING_SOURCE}:
        code = _first_text(row, "PRK_CD")
        name = _first_text(row, "PRK_NM")
        lon_value = _first_text(row, "LNG", "LON")
        address = _first_text(row, "ROAD_ADDR", "ADDRESS")
        updated_at = row.get("CUR_PRK_TIME")
        detail_keys = (
            "PRK_TYPE",
            "CPCTY",
            "CUR_PRK_CNT",
            "CUR_PRK_TIME",
            "CUR_PRK_YN",
            "NOW_PRK_VHCL_CNT",
            "TPKCT",
            "PRK_STTS_YN",
            "NOW_PRK_VHCL_UPDT_TM",
            "PAY_YN",
            "RATES",
            "TIME_RATES",
            "ADD_RATES",
            "ADD_TIME_RATES",
        )
    else:
        code = _first_text(row, "PKLT_CD")
        name = _first_text(row, "PKLT_NM")
        lon_value = _first_text(row, "LOT")
        address = _first_text(row, "ADDR")
        updated_at = row.get("LAST_DATA_SYNC_TM")
        detail_keys = (
            "TELNO",
            "TPKCT",
            "CHGD_FREE_NM",
            "PRK_CRG",
            "PRK_HM",
            "ADD_CRG",
            "ADD_UNIT_TM_MNT",
            "WD_OPER_BGNG_TM",
            "WD_OPER_END_TM",
            "WE_OPER_BGNG_TM",
            "WE_OPER_END_TM",
            "LHLDY_BGNG",
            "LHLDY",
            "LAST_DATA_SYNC_TM",
            "CRB_PKLT_MNG_GROUP_NO",
        )
    if not code or not name:
        raise ValueError("missing_identity")
    try:
        lat, lon = float(row["LAT"]), float(lon_value)
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
    details = {key: row.get(key) for key in detail_keys}
    return {
        "id": hashlib.sha256(identity.encode()).hexdigest(),
        "source": source,
        "source_id": code,
        "name": name,
        "address": address,
        "location": f"SRID=4326;POINT({lon} {lat})",
        "details": details,
        "source_updated_at": source_time(updated_at),
        "collected_at": collected_at,
    }
