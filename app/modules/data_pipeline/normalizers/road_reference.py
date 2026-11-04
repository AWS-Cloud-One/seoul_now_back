"""Official road input evidence and quality; no database or file access."""

from collections import defaultdict
from datetime import date

from app.modules.data_pipeline.identity import fingerprint
from app.modules.data_pipeline.normalizers.official_files import (
    complete_vertices,
    topis_mapping,
    topis_vertices,
)

HEADERS = {
    "vertices": {"A": "LINK_ID", "B": "VER_SEQ", "C": "GRS80TM_X", "D": "GRS80TM_Y"},
    "mapping": {"A": "서비스링크", "B": "표준링크순서", "C": "표준링크아이디"},
}
SOURCE_URLS = {
    "vertices": "https://data.seoul.go.kr/dataList/OA-15060/F/1/datasetView.do",
    "mapping": "https://data.seoul.go.kr/dataList/OA-15061/S/1/datasetView.do",
}


def reference_metadata(
    data,
    *,
    vertices_version,
    mapping_version,
    vertices_date=None,
    mapping_date=None,
    source_srid=5181,
    complete=True,
    allowed_exclusions=(),
):
    # TOPIS's official drawLink/makeLineLayer implementation uses GRS80TM as EPSG:5181.
    # CITYDATA comparisons still detect wrong/swapped coordinates and incompatible input files.
    if source_srid != 5181:
        raise ValueError("unsupported_reference_crs")
    inputs = {}
    for name, version, reference_date in (
        ("vertices", vertices_version, vertices_date),
        ("mapping", mapping_version, mapping_date),
    ):
        if not isinstance(version, str) or not version.strip():
            raise ValueError("missing_reference_version")
        if reference_date is not None:
            reference_date = date.fromisoformat(reference_date).isoformat()
        file = data[name]
        inputs[name] = {key: file[key] for key in ("filename", "sha256", "byte_size", "header")} | {
            "version": version,
            "reference_date": reference_date,
            "source_url": SOURCE_URLS[name],
        }
    identity = {
        "inputs": inputs,
        "source_crs": f"EPSG:{source_srid}",
        "allowed_exclusions": sorted(set(allowed_exclusions)),
    }
    return {**identity, "complete": complete, "reference_id": fingerprint(identity)}


def prepare_reference(data):
    for name, expected in HEADERS.items():
        if any(data[name]["header"].get(k) != v for k, v in expected.items()):
            raise ValueError(f"invalid_{name}_header")
    vertex_rows = [r for r in data["vertices"]["rows"] if r]
    mapping_input = [r for r in data["mapping"]["rows"] if r]
    all_points = topis_vertices(vertex_rows)
    points, excluded, total = complete_vertices(all_points)
    mappings = topis_mapping(mapping_input)
    if not mappings:
        raise ValueError("empty_reference_mapping")
    valid_links = {p["link_id"] for p in points}
    accepted = [r for r in mappings if r["service_link_id"] in valid_links]
    unmatched = [r for r in mappings if r["service_link_id"] not in valid_links]
    groups = defaultdict(set)
    for row in mappings:
        if row["sequence_status"] == "unknown":
            continue
        groups[row["service_link_id"], row["sequence"]].add(row["standard_link_id"])
    ambiguous = [
        {"service_link_id": link, "sequence": sequence, "standard_link_ids": sorted(ids)}
        for (link, sequence), ids in sorted(groups.items())
        if len(ids) > 1
    ]
    report = {
        "file_links": total,
        "geometry_links": len(valid_links),
        "excluded_sequence_gap_links": excluded,
        "vertex_sheet_rows": len(data["vertices"]["rows"]),
        "blank_vertex_rows": len(data["vertices"]["rows"]) - len(vertex_rows),
        "vertex_rows": len(vertex_rows),
        "vertices": len(points),
        "duplicate_vertex_rows": len(vertex_rows) - len(all_points),
        "mapping_sheet_rows": len(data["mapping"]["rows"]),
        "blank_mapping_rows": len(data["mapping"]["rows"]) - len(mapping_input),
        "mapping_rows": len(mapping_input),
        "mapping_unique_rows": len(mappings),
        "duplicate_mapping_rows": len(mapping_input) - len(mappings),
        "accepted_mapping_rows": len(accepted),
        "unmatched_mapping_rows": unmatched,
        "geometry_without_mapping": sorted(valid_links - {r["service_link_id"] for r in accepted}),
        "ambiguous_mapping_positions": ambiguous,
        "unknown_sequence_rows": [r for r in mappings if r["sequence_status"] == "unknown"],
    }
    return points, mappings, report


def reference_error(data, *, require_spatial=False):
    """A complete input is distinct from usable rows and reviewed exclusions within it."""
    ref, report = data.get("reference") or {}, data.get("report") or {}
    if data.get("validation_error"):
        return data["validation_error"]
    if ref.get("complete") is not True:
        return "incomplete_road_reference"
    inputs = ref.get("inputs", {})
    if any(
        not inputs.get(k, {}).get("version") or not inputs[k].get("sha256")
        for k in ("vertices", "mapping")
    ):
        return "missing_reference_provenance"
    features = data.get("features", [])
    ids = {f["id"] for f in features}
    if not features or len(ids) != len(features):
        return "empty_or_duplicate_road_reference"
    excluded = set(report.get("excluded_sequence_gap_links", []))
    quarantined = set(report.get("quarantined_service_ids", []))
    if excluded != set(ref.get("allowed_exclusions", [])):
        return "unreviewed_reference_exclusions"
    if quarantined and (
        quarantined & (ids | excluded)
        or quarantined
        != set(report.get("spatial_validation", {}).get("quarantined_service_ids", []))
    ):
        return "inconsistent_quarantine_evidence"
    if report.get("file_links") != len(features) + len(excluded) + len(quarantined):
        return "inconsistent_reference_counts"
    if any(r["service_link_id"] not in ids | excluded for r in data.get("mapping_rows", [])):
        return "unmatched_reference_mapping"
    if require_spatial:
        spatial = report.get("spatial_validation", {})
        if spatial.get("status") != "checked_available_references" or spatial.get("error_code"):
            return "missing_or_failed_spatial_validation"
    return None
