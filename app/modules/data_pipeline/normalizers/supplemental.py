"""Supplemental field conversion and snapshot metadata without HTTP or SQL."""

import math


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
