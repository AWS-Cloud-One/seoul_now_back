"""Supplemental field conversion and snapshot metadata without HTTP or SQL."""


def area_feature(district, row, geometry):
    return {
        "type": "Feature",
        "id": row["cd"],
        "geometry": geometry,
        "properties": {
            "code": row["cd"],
            "name": row["addr_name"],
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
