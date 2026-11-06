"""Pure validation and feature construction for official road/place files."""

import math
import re


def citydata_areas(rows):
    """Reject conflicting codes instead of silently taking the last spreadsheet row."""
    result = {}
    for row in rows:
        values = list(row.values())
        for index, value in enumerate(values):
            if not isinstance(value, str) or not re.fullmatch(r"POI\d{3}", value):
                continue
            name = values[index + 1] if index + 1 < len(values) else None
            if not isinstance(name, str) or not name.strip():
                raise ValueError("official_area_list_invalid")
            name = name.strip()
            if value in result and result[value] != name:
                raise ValueError("conflicting_official_area")
            result[value] = name
    if not result or len(set(result.values())) != len(result):
        raise ValueError("official_area_list_invalid")
    return result


def validate_place_reference(targets, shapes):
    """The list and boundary archive must describe exactly the same places."""
    codes = [values["AREA_CD"] for values, _ in shapes]
    if len(codes) != len(set(codes)) or set(codes) != set(targets):
        raise ValueError("place_reference_code_mismatch")
    if any(values["AREA_NM"].strip() != targets[values["AREA_CD"]] for values, _ in shapes):
        raise ValueError("place_reference_name_mismatch")


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
    pairs = {}
    groups = {}
    for values in rows:
        if not values:
            continue
        service, standard = values.get("A", ""), values.get("C", "")
        original = values.get("B")
        unknown = original is None or (isinstance(original, str) and not original.strip())
        try:
            seq = 0 if unknown else int(original)
        except ValueError, TypeError:
            raise ValueError("invalid_official_mapping_sequence") from None
        if not re.fullmatch(r"\d{10}", service) or not re.fullmatch(r"\d{10}", standard) or seq < 0:
            raise ValueError("invalid_official_mapping")
        key = (service, seq, standard)
        pairs[key] = pairs.get(key, False) or unknown
        if not unknown:
            groups.setdefault((service, seq), set()).add(standard)
    return [
        {
            "service_link_id": service,
            "sequence": seq,
            "standard_link_id": standard,
            "sequence_status": "unknown"
            if unknown
            else "ambiguous"
            if len(groups[service, seq]) > 1
            else "known",
        }
        for (service, seq, standard), unknown in sorted(pairs.items())
    ]


def road_features(lines, mapping, *, version, source_srid):
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
                    "source_version": version,
                    "source_crs": f"EPSG:{source_srid}",
                },
            }
        )
    return features


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
