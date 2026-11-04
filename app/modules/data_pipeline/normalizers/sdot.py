"""S-DoT installation and observation quality; no HTTP, database or implicit clock."""

import math
import re
from collections import Counter, defaultdict
from datetime import UTC

from app.modules.data_pipeline.identity import fingerprint
from app.modules.data_pipeline.normalizers.values import FieldValue, number, timestamp

HEADERS = {
    "provider_sensor_id": "방문자 센서코드",
    "device_serial": "시리얼번호",
    "address": "주소",
    "lat": "위도",
    "lng": "경도",
}
OBSERVATION_FIELDS = ("SERIAL", "SENSING_TIME", "VISITOR_COUNT", "DATE", "DATA_NO", "MODELNAME")


def sensor_code(value):
    # Identifier syntax is deliberately stricter than numerical measurement syntax.
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    value = str(value).strip()
    return str(int(value)) if re.fullmatch(r"[0-9]+", value) and int(value) > 0 else None


def identifier(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    return str(value).strip() or None


def sensor_locations(file):
    """Resolve headers by name and quarantine all variants of a conflicting sensor code."""
    header = file["header"]
    columns = {}
    for field, name in HEADERS.items():
        matches = [k for k, v in header.items() if isinstance(v, str) and v.strip() == name]
        if len(matches) != 1:
            raise ValueError("invalid_sensor_file_header")
        columns[field] = matches[0]
    candidates, issues = defaultdict(list), []
    for index, row in enumerate(file["rows"]):
        item = {field: row.get(column) for field, column in columns.items()}
        code = sensor_code(item["provider_sensor_id"])
        reason = None
        try:
            if any(isinstance(item[k], bool) for k in ("lat", "lng")):
                raise ValueError
            lat, lng = float(item["lat"]), float(item["lng"])
            if not (
                math.isfinite(lat)
                and math.isfinite(lng)
                and -90 <= lat <= 90
                and -180 <= lng <= 180
            ):
                raise ValueError
        except TypeError, ValueError:
            lat = lng = None
            reason = "invalid_coordinates"
        if not code:
            reason = "invalid_sensor_code"
        elif not isinstance(item["address"], str) or not item["address"].strip():
            reason = "missing_address"
        elif not item["address"].strip().startswith("서울"):
            reason = "outside_seoul_address"
        elif not identifier(item["device_serial"]):
            reason = "missing_device_serial"
        if reason:
            issues.append({"row": index, "sensor_code": code, "reason": reason})
        if code:
            candidates[code].append(
                (
                    index,
                    {
                        "id": code,
                        "provider_sensor_id": str(item["provider_sensor_id"]),
                        "device_serial": identifier(item["device_serial"]),
                        "address": item["address"].strip()
                        if isinstance(item["address"], str)
                        else None,
                        "lat": lat,
                        "lng": lng,
                    },
                    reason,
                )
            )
    locations, duplicates = {}, 0
    for code, rows in sorted(candidates.items()):
        facts = {
            fingerprint({k: v for k, v in item.items() if k != "provider_sensor_id"})
            for _, item, _ in rows
        }
        if len(facts) > 1 or any(reason for _, _, reason in rows):
            if len(rows) > 1:
                issues.append(
                    {
                        "sensor_code": code,
                        "rows": [i for i, _, _ in rows],
                        "reason": "conflicting_location",
                    }
                )
            continue
        item = dict(rows[0][1])
        item["provider_sensor_ids"] = sorted({v["provider_sensor_id"] for _, v, _ in rows})
        item["provider_sensor_id"] = item["provider_sensor_ids"][0]
        item["source_rows"] = [i for i, _, _ in rows]
        locations[code] = item
        duplicates += len(rows) - 1
    return {
        "locations": locations,
        "metadata": {k: v for k, v in file.items() if k not in {"body", "rows"}},
        "quality": {
            "received": len(file["rows"]),
            "accepted": len(locations),
            "duplicates": duplicates,
            "rejected": len(file["rows"]) - len(locations) - duplicates,
            "issues": issues,
        },
    }


def observation_rows(locations, collected):
    """Retain selected provider fields plus page receipt; never substitute a processing time."""
    result = []
    for index, row in enumerate(collected["rows"]):
        context = collected["row_contexts"][index]
        receipt = timestamp(context.get("received_at"))
        observed = timestamp(row.get("SENSING_TIME"), not_after=receipt.value)
        provider_time = timestamp(row.get("DATE"), not_after=receipt.value)
        count = number(row.get("VISITOR_COUNT"), integer=True)
        if count.value is not None and count.value > 9223372036854775807:
            count = FieldValue(reason="INTEGER_OUT_OF_RANGE")
        code, data_no = sensor_code(row.get("SERIAL")), identifier(row.get("DATA_NO"))
        issues = {
            key: value.reason
            for key, value in (
                ("observed_at", observed),
                ("provider_collected_at", provider_time),
                ("count_10min", count),
                ("received_at", receipt),
            )
            if value.reason
        }
        reason = None
        if code not in locations:
            reason = "unregistered_sensor" if code else "invalid_sensor_code"
        elif not data_no:
            reason = "missing_or_invalid_data_no"
        elif observed.reason:
            reason = "invalid_observation_time"
        elif count.reason:
            reason = "invalid_observation_count"
        elif provider_time.state == "invalid":
            reason = "invalid_provider_time"
        elif provider_time.value and observed.value > provider_time.value:
            reason = "provider_time_before_observation"
        result.append(
            {
                "input_index": index,
                "input": {k: row.get(k) for k in OBSERVATION_FIELDS},
                **context,
                "sensor_code": code,
                "data_no": data_no,
                "observed_at": observed.value.isoformat() if observed.value else None,
                "provider_collected_at": provider_time.value.isoformat()
                if provider_time.value
                else None,
                "count_10min": count.value,
                "field_issues": issues,
                "status": "rejected" if reason else "accepted",
                "reason": reason,
            }
        )
    return result


def latest_sensors(locations, rows):
    groups = defaultdict(list)
    for row in rows:
        if row["status"] == "accepted":
            groups[row["sensor_code"]].append(row)
    items = []
    for code, location in sorted(locations.items()):
        candidates = groups[code]
        item = {
            **location,
            "count_10min": None,
            "observed_at": None,
            "provider_collected_at": None,
            "data_no": None,
            "observation_status": "unobserved",
            "usable": False,
            "input_indexes": [],
        }
        if candidates:
            by_time = defaultdict(list)
            for row in candidates:
                by_time[timestamp(row["observed_at"]).value.astimezone(UTC)].append(row)
            selected = {}
            for moment, group in by_time.items():
                dated = [r for r in group if r["provider_collected_at"]]
                latest_date = max(
                    (timestamp(r["provider_collected_at"]).value for r in dated), default=None
                )
                current = (
                    [r for r in dated if timestamp(r["provider_collected_at"]).value == latest_date]
                    if dated
                    else group
                )
                for row in group:
                    row["selection"] = "current" if row in current else "superseded"
                if len({r["count_10min"] for r in current}) > 1:
                    for row in current:
                        row.update(status="rejected", reason="conflicting_observation")
                selected[moment] = current
            latest = selected[max(by_time)]
            # DATA_NO is preserved as a source tag, never ranked as a sensor ID or revision.
            # Equal counts/dates with multiple tags use one deterministic representative.
            representative = min(latest, key=lambda r: (r["data_no"], fingerprint(r["input"])))
            evidence = [r for r in latest if r["data_no"] == representative["data_no"]]
            item.update(
                observed_at=max(by_time).isoformat(),
                data_no=representative["data_no"],
                data_no_variants=sorted({r["data_no"] for r in latest}),
                input_indexes=[r["input_index"] for r in evidence],
            )
            if any(r["reason"] == "conflicting_observation" for r in latest):
                item["observation_status"] = "conflicting"
                item["input_indexes"] = [r["input_index"] for r in latest]
            else:
                provider_time = timestamp(representative["provider_collected_at"]).value
                issues = {k: v for row in latest for k, v in row["field_issues"].items()}
                item.update(
                    count_10min=representative["count_10min"],
                    provider_collected_at=provider_time.astimezone(UTC).isoformat()
                    if provider_time
                    else None,
                    observation_status="partial" if issues else "valid",
                    usable=not issues,
                    field_issues=issues,
                )
        elif any(r["sensor_code"] == code for r in rows):
            item["observation_status"] = "invalid"
        items.append(item)
    # Only equal selected facts count as duplicates; conflicting records remain rejected evidence.
    seen = set()
    for row in rows:
        digest = fingerprint(row["input"])
        if row["status"] == "accepted":
            if digest in seen:
                row["status"] = "duplicate"
            seen.add(digest)
    return items


def sdot_snapshot(reference, collected):
    observations = observation_rows(reference["locations"], collected)
    items = latest_sensors(reference["locations"], observations)
    counts = Counter(r["status"] for r in observations)
    reasons = Counter(r["reason"] for r in observations if r["reason"])
    return {
        "items": items,
        "source": "서울 S-DoT",
        "reference": reference["metadata"],
        "location_quality": reference["quality"],
        "input_rows": observations,
        "received": len(observations),
        "provider_total": collected["provider_total"],
        "query": collected["query"],
        "pages": [{k: v for k, v in page.items() if k != "body"} for page in collected["pages"]],
        "collection_issues": collected["issues"],
        "query_complete": collected["query_complete"],
        "complete_history": False,
        "latest_all_sensors_verified": False,
        "scope": "bounded_query_window_client_time_order",
        "observation_policy": {
            "selection": "latest SENSING_TIME, then latest DATE within the same sensor/time",
            "data_no": "preserved tag; no numeric priority",
            "interval_minutes": 10,
            "interval_boundary": "unspecified; provider timestamp retained without shifting",
        },
        "observation_quality": {
            "unit": "observation_row",
            "scope": "sdot_preprocessing",
            "received": len(observations),
            "accepted": counts["accepted"],
            "rejected": counts["rejected"],
            "duplicates": counts["duplicate"],
            "rejection_reasons": dict(reasons),
        },
        "notice": (
            "센서의 10분 통행량. 조회 창에 없는 관측은 미확인이고 "
            "전 센서 최신값 확보 여부는 알 수 없습니다."
        ),
    }


def observation_summary(items):
    return {
        "locations": len(items),
        "with_count": sum(i["count_10min"] is not None for i in items),
        "usable_observations": sum(i["usable"] for i in items),
        "states": dict(Counter(i["observation_status"] for i in items)),
    }
