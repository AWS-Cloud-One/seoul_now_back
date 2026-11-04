"""Road-reference geometry quality and conservative matching decisions; no DB or I/O."""

import math
from collections import Counter, defaultdict

from app.modules.data_pipeline.identity import fingerprint

# Internal validation policy, not accuracy limits promised by either provider.
MATCH_POLICY = {
    "version": "road-shape-v3",
    "metric_srid": 5181,
    "tolerance_m": 30.0,
    "segment_length_m": 10.0,
    "minimum_coverage": 0.9,
    "minimum_length_ratio": 0.8,
    "maximum_length_ratio": 1.25,
    "minimum_extent_m": 1.0,
    "minimum_reference_links": 10,
    "minimum_matched_pair_fraction": 0.9,
}


def citydata_line(row):
    text = row.get("XYLIST")
    if not isinstance(text, str) or not text.strip():
        return None, "missing_coordinates"
    try:
        xy = [tuple(float(v) for v in p.split("_")) for p in text.split("|")]
    except ValueError:
        return None, "invalid_coordinates"
    if any(
        len(p) != 2
        or not all(math.isfinite(v) for v in p)
        or not (126 <= p[0] <= 128 and 37 <= p[1] <= 38)
        for p in xy
    ):
        return None, "invalid_coordinates"
    xy = [p for i, p in enumerate(xy) if not i or p != xy[i - 1]]
    if len(xy) < 2:
        return None, "degenerate_line"
    return {"type": "LineString", "coordinates": xy}, None


def citydata_geometry_index(inputs):
    """Exact normalized duplicates coalesce; conflicting variants never use last-row-wins."""
    variants, issues, sources = defaultdict(dict), defaultdict(set), defaultdict(list)
    section_issues = []
    for item in inputs:
        evidence = {k: item.get(k) for k in ("area_code", "payload_id", "received_at")}
        traffic = item["data"].get("ROAD_TRAFFIC_STTS")
        rows = traffic.get("ROAD_TRAFFIC_STTS") if isinstance(traffic, dict) else None
        if not isinstance(rows, list):
            section_issues.append({**evidence, "reason": "missing_or_invalid_traffic_section"})
            continue
        for index, row in enumerate(rows):
            link = row.get("LINK_ID") if isinstance(row, dict) else None
            if not isinstance(link, str) or not link.strip():
                section_issues.append({**evidence, "row": index, "reason": "missing_link_id"})
                continue
            geometry, error = citydata_line(row)
            digest = fingerprint(geometry) if geometry else None
            sources[link].append(
                {**evidence, "row": index, "geometry_hash": digest, "reason": error}
            )
            if error:
                issues[link].add(error)
            else:
                variants[link][digest] = geometry
    result = {}
    for link in sorted(sources):
        options = variants[link]
        state = "conflict" if len(options) > 1 else "invalid" if issues[link] else "valid"
        result[link] = {
            "state": state,
            "geometry": next(iter(options.values())) if state == "valid" else None,
            "geometry_hashes": sorted(options),
            "issues": sorted(issues[link]),
            "sources": sorted(
                sources[link],
                key=lambda s: (
                    str(s["payload_id"]),
                    str(s["area_code"]),
                    s["row"],
                    str(s["geometry_hash"]),
                ),
            ),
        }
    return result, section_issues


def classify_match(metrics):
    p = MATCH_POLICY
    if min(metrics["service_length_m"], metrics["standard_length_m"]) < p["minimum_extent_m"]:
        status = "insufficient_extent"
    elif not metrics["simple_lines"]:
        status = "ambiguous_curve"
    elif metrics["minimum_distance_m"] > p["tolerance_m"]:
        status = "separated"
    elif metrics["span_ratio"] < p["minimum_length_ratio"]:
        status = "crossing_or_touch_only" if metrics["span_ratio"] < 0.05 else "partial_overlap"
    elif metrics["coverage_ratio"] < p["minimum_coverage"]:
        status = "partial_overlap" if metrics["coverage_ratio"] > 0 else "separated"
    elif (
        metrics["span_ratio"] > p["maximum_length_ratio"]
        or metrics["shape_distance_m"] > p["tolerance_m"]
    ):
        status = "shape_mismatch"
    else:
        status = "matched"
    return {
        **metrics,
        "status": status,
        "coordinate_order": "reversed"
        if metrics["end_fraction"] < metrics["start_fraction"]
        else "same"
        if metrics["end_fraction"] > metrics["start_fraction"]
        else "undetermined",
        "extent": "standard_within_service"
        if metrics["standard_length_m"] <= metrics["service_length_m"]
        else "service_within_standard",
    }


def validation_outcome(pairs, index, total_roads, mapped_standard_ids, section_issues):
    available = set(index) & mapped_standard_ids
    conflicts = sorted(link for link in available if index[link]["state"] == "conflict")
    invalid = sorted(link for link in available if index[link]["state"] == "invalid")
    counts = dict(Counter(p["status"] for p in pairs))
    matched = [p for p in pairs if p["status"] == "matched"]
    compared_services = {p["service_link_id"] for p in pairs}
    mismatched_services = {p["service_link_id"] for p in pairs if p["status"] != "matched"}
    services = {p["service_link_id"] for p in matched} - mismatched_services
    standards = {p["standard_link_id"] for p in matched}
    geometries = {h for link in standards for h in index[link]["geometry_hashes"]}
    # Official service geometry and standard-link extents need not share their endpoints.
    # Keep a small set of suspect service links out of the active reference. A broadly
    # incompatible file still fails the unchanged 90%/minimum evidence requirements.
    partial = [p for p in pairs if p["status"] == "partial_overlap"]
    hard_mismatches = [p for p in pairs if p["status"] not in {"matched", "partial_overlap"}]
    quarantined = sorted({p["service_link_id"] for p in hard_mismatches})
    matched_fraction = len(matched) / len(pairs) if pairs else 0
    if conflicts:
        error = "conflicting_spatial_reference"
    elif invalid:
        error = "invalid_spatial_reference"
    elif not index:
        error = "missing_spatial_reference"
    elif not available:
        error = "missing_spatial_correspondence"
    elif (hard_mismatches or partial) and (
        matched_fraction < MATCH_POLICY["minimum_matched_pair_fraction"]
    ):
        error = "road_geometry_mismatch"
    elif (
        min(len(services), len(standards), len(geometries))
        < MATCH_POLICY["minimum_reference_links"]
    ):
        error = "insufficient_spatial_reference"
    else:
        error = None
    coordinates = [
        xy for item in index.values() if item["geometry"] for xy in item["geometry"]["coordinates"]
    ]
    return {
        "status": "held" if error else "checked_available_references",
        "error_code": error,
        "policy": dict(MATCH_POLICY),
        "provider_epsg_confirmed": True,
        "crs_basis": "TOPIS drawLink grs80tmX/Y -> makeLineLayer EPSG:5181; CITYDATA cross-check",
        "crs_evidence": [
            "https://topis.seoul.go.kr/js/map/map.js",
            "https://topis.seoul.go.kr/js/map/layer.js",
        ],
        "direction_verified": False,
        "all_roads_verified": False,
        "reference_links": len(index),
        "compared_pairs": len(pairs),
        "pair_status_counts": counts,
        "matched_pair_fraction": matched_fraction,
        "advisory_pair_count": len(partial),
        "quarantined_service_ids": quarantined,
        "quarantined_pairs": hard_mismatches,
        "warnings": (["partial_overlap_in_official_mapping"] if partial else [])
        + (["quarantined_service_links"] if quarantined else [])
        + (["unmapped_standard_links"] if set(index) - mapped_standard_ids else []),
        "compared_service_links": len(compared_services),
        "checked_service_links": len(services),
        "mismatched_service_ids": sorted(mismatched_services),
        "uncompared_service_links": total_roads - len(compared_services),
        "checked_standard_links": len(standards),
        "unverified_service_links": total_roads - len(services),
        "unmapped_standard_ids": sorted(set(index) - mapped_standard_ids),
        "conflicting_standard_ids": conflicts,
        "invalid_standard_ids": invalid,
        "reference_geometry_count": len(geometries),
        "reference_bounds": [
            min(x for x, _ in coordinates),
            min(y for _, y in coordinates),
            max(x for x, _ in coordinates),
            max(y for _, y in coordinates),
        ]
        if coordinates
        else None,
        "section_issues": section_issues,
        "pair_results": pairs,
        "reference_evidence": {
            link: {k: v for k, v in item.items() if k != "geometry"} for link, item in index.items()
        },
    }
