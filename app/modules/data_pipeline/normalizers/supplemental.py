"""Supplemental field conversion and snapshot metadata without HTTP or SQL."""

import math
from datetime import datetime


def sensor_locations(rows):
    result = {}
    for values in rows:
        if not str(values.get("D", "")).startswith("서울"):
            continue
        try:
            code = str(int(values["B"]))
            lat, lng = float(values["E"]), float(values["F"])
            if not (math.isfinite(lat) and math.isfinite(lng)):
                continue
            result[code] = {"id": code, "address": values["D"], "lat": lat, "lng": lng}
        except KeyError, ValueError, TypeError:
            continue
    return result


def latest_sensors(locations, rows):
    from zoneinfo import ZoneInfo

    result = {
        code: {**value, "count_10min": None, "observed_at": None}
        for code, value in locations.items()
    }
    conflicts = set()
    seen = {}
    for row in rows:
        try:
            code = str(int(row["SERIAL"]))
            when = datetime.fromisoformat(row["SENSING_TIME"].replace("_", "T")).replace(
                tzinfo=ZoneInfo("Asia/Seoul")
            )
            count = int(row["VISITOR_COUNT"])
            if count < 0 or code not in result:
                continue
        except ValueError, KeyError, TypeError:
            continue
        key = (code, when.isoformat())
        if key in seen and seen[key] != count:
            conflicts.add(key)
        seen[key] = count
        item = result[code]
        if not item["observed_at"] or when.isoformat() > item["observed_at"]:
            item.update(count_10min=count, observed_at=when.isoformat())
    for item in result.values():
        if (item["id"], item["observed_at"]) in conflicts:
            item.update(count_10min=None, observation_status="conflicting")
    return list(result.values())


def area_feature(district, row, geometry):
    return {
        "type": "Feature",
        "id": row["cd"],
        "geometry": geometry,
        "properties": {
            "code": row["cd"],
            "name": row["addr_name"],
            "full_name": row["full_addr"],
            "district": district["addr_name"],
            "sigungu_code": district["cd"],
        },
    }


def area_snapshot(features):
    return {
        "type": "FeatureCollection",
        "features": features,
        "source": "SGIS addr/stage",
        "boundary_year": None,
        "notice": "조회 시 제공된 행정동 경계. 제공 응답에 경계 기준연도 없음. SGIS 코드 체계.",
    }


def boundary_snapshot(features):
    return {
        "type": "FeatureCollection",
        "features": features,
        "boundary_year": 2025,
        "source": "SGIS",
        "notice": "2025 통계 경계 기준 서울 소재지 필터",
    }


def sdot_snapshot(locations, collected):
    rows, total = collected["rows"], collected["provider_total"]
    return {
        "items": latest_sensors(locations, rows),
        "source": "서울 S-DoT",
        "installation_file_date": "2025-11-13",
        "received": len(rows),
        "provider_total": total,
        "scope": "recent_10000_rows",
        "complete_history": len(rows) >= total,
        "notice": (
            "센서 지점의 10분 통행량이며 주변 체류 인구나 혼잡 등급이 아닙니다. "
            "최근 조회 창에 없는 관측은 미확인입니다."
        ),
    }


def cctv_candidates(rows):
    items = []
    for row in rows:
        try:
            lng, lat = float(row["coordx"]), float(row["coordy"])
            if not math.isfinite(lng) or not math.isfinite(lat):
                continue
        except ValueError, TypeError, KeyError:
            continue
        items.append(
            {
                "roadsectionid": row.get("roadsectionid"),
                "name": row.get("cctvname"),
                "lat": lat,
                "lng": lng,
                "url": row.get("cctvurl"),
                "format": row.get("cctvformat"),
                "direction": None,
                "playback_status": "unverified",
            }
        )
    return items


def cctv_snapshot(items, collected):
    selected = []
    for index, item in enumerate(items):
        item = dict(item)
        item["id"] = f"{item.pop('roadsectionid')}:{index}"
        selected.append(item)
    return {
        "items": selected,
        "received": len(collected["rows"]),
        "source": "ITS CCTV",
        "issued_at": collected["fetched_at"].isoformat(),
        "filter_boundary_year": 2025,
        "notice": (
            "위치·영상 주소 확인 단계. 재생·영상 갱신·촬영 방향은 미검증. "
            "클릭 시 ITS 목록을 재호출하지 않습니다."
        ),
    }
