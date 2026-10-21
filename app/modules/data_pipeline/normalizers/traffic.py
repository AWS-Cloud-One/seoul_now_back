"""Road geometry from official citydata, without guessing missing coordinates."""

import math


def road_feature(row, area_code, collected_at, source_time):
    try:
        coordinates = [
            [float(v) for v in point.split("_")]
            for point in str(row.get("XYLIST") or "").split("|")
        ]
        if len(coordinates) < 2 or any(
            len(p) != 2
            or not all(math.isfinite(v) for v in p)
            or not (126 <= p[0] <= 128 and 37 <= p[1] <= 38)
            for p in coordinates
        ):
            return None
        link = str(row["LINK_ID"]).strip()
        if not link:
            return None
    except ValueError, TypeError, KeyError:
        return None
    return {
        "type": "Feature",
        "id": link,
        "geometry": {"type": "LineString", "coordinates": coordinates},
        "properties": {
            "link_id": link,
            "road_name": row.get("ROAD_NM"),
            "speed_kmh": row.get("SPD"),
            "traffic_status": row.get("IDX"),
            "area_code": area_code,
            "source": "seoul_citydata",
            "source_time": source_time,
            "collected_at": collected_at,
            "start_name": row.get("START_ND_NM"),
            "end_name": row.get("END_ND_NM"),
        },
    }
