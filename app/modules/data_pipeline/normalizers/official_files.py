"""Pure validation and feature construction for official road/place files."""

import math
import re


def topis_vertices(rows):
    seen = {}
    for cells in rows:
        if not cells:
            continue
        link = cells.get("A", "")
        if not re.fullmatch(r"\d{10}", link):
            raise ValueError("invalid_official_link")
        seq_value = float(cells["B"])
        x, y = float(cells["C"]), float(cells["D"])
        if (
            not all(math.isfinite(n) for n in (seq_value, x, y))
            or not seq_value.is_integer()
            or seq_value < 1
        ):
            raise ValueError("invalid_vertex")
        key = (link, int(seq_value))
        if key in seen and seen[key] != (x, y):
            raise ValueError("conflicting_vertex_sequence")
        seen[key] = (x, y)
    groups = {}
    for link, seq in seen:
        groups.setdefault(link, []).append(seq)
    if not groups or any(len(seq) < 2 for seq in groups.values()):
        raise ValueError("incomplete_vertex_sequence")
    return [
        {"link_id": link, "seq": seq, "x": xy[0], "y": xy[1]}
        for (link, seq), xy in sorted(seen.items())
    ]


def complete_vertices(points):
    sequences = {}
    for point in points:
        sequences.setdefault(point["link_id"], []).append(point["seq"])
    excluded = sorted(
        link for link, seq in sequences.items() if sorted(seq) != list(range(1, len(seq) + 1))
    )
    excluded_set = set(excluded)
    return [p for p in points if p["link_id"] not in excluded_set], excluded, len(sequences)


def topis_mapping(rows):
    services = {}
    for values in rows:
        if not values:
            continue
        service, standard = values["A"], values["C"]
        seq = int(values.get("B") or 0)
        if not re.fullmatch(r"\d{10}", service) or not re.fullmatch(r"\d{10}", standard) or seq < 0:
            raise ValueError("invalid_official_mapping")
        services.setdefault(service, []).append((seq, standard))
    return [
        {"service_link_id": service, "sequence": seq, "standard_link_id": standard}
        for service, pairs in services.items()
        for seq, standard in sorted(set(pairs))
    ]


def road_features(lines, mapping):
    features = []
    for link, geometry in lines:
        if not all(126 <= x <= 128 and 37 <= y <= 38 for x, y in geometry["coordinates"]):
            raise ValueError("transformed_geometry_out_of_region")
        features.append(
            {
                "type": "Feature",
                "id": link,
                "geometry": geometry,
                "properties": {
                    "service_link_id": link,
                    "standard_link_ids": mapping.get(link, []),
                    "geometry_source": "OA-15060",
                    "source_year": 2025,
                    "source_crs": "EPSG:5181",
                },
            }
        )
    return features


def citydata_links(payloads):
    links = {}
    for data in payloads:
        traffic = data.get("ROAD_TRAFFIC_STTS") or {}
        if isinstance(traffic, dict):
            for row in traffic.get("ROAD_TRAFFIC_STTS") or []:
                if isinstance(row, dict) and row.get("LINK_ID"):
                    links[str(row["LINK_ID"])] = row
    return links


def citydata_line(row):
    try:
        xy = [[float(v) for v in p.split("_")] for p in row["XYLIST"].split("|")]
        return {"type": "LineString", "coordinates": xy} if len(xy) >= 2 else None
    except KeyError, ValueError, TypeError:
        return None


def rings_wkt(rings):
    return (
        "MULTILINESTRING("
        + ",".join("(" + ",".join(f"{x} {y}" for x, y in ring) + ")" for ring in rings)
        + ")"
    )


def place_feature(values, geometry, repaired):
    return {
        "type": "Feature",
        "id": values["AREA_CD"],
        "geometry": geometry,
        "properties": {
            "area_code": values["AREA_CD"],
            "name": values["AREA_NM"],
            "category": values["CATEGORY"],
            "topology_repaired": repaired,
        },
    }
