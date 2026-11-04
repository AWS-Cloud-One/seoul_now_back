"""CITYDATA geometry quality, conflicts, validation scope and decisions."""

from copy import deepcopy

import pytest

from app.modules.data_pipeline.normalizers.road_geometry import (
    citydata_geometry_index,
    citydata_line,
    validation_outcome,
)


def source(rows, payload="p1"):
    return {
        "data": {"ROAD_TRAFFIC_STTS": {"ROAD_TRAFFIC_STTS": rows}},
        "area_code": "POI001",
        "payload_id": payload,
        "received_at": None,
    }


def row(xy="127_37.5|127.01_37.5"):
    return {"LINK_ID": "1", "XYLIST": xy}


@pytest.mark.parametrize(
    "xy",
    [
        None,
        "",
        1,
        [],
        "NaN_37|127_37.5",
        "Infinity_37|127_37.5",
        "127_37.5_2|127.01_37.5",
        "37.5_127|37.6_127",
        "127_37.5|127_37.5",
        "127_37.5|broken",
        "127_37.5|",
    ],
)
def test_invalid_reference_coordinates_are_not_forwarded_to_postgis(xy):
    geometry, error = citydata_line(row(xy))
    assert geometry is None and error


def test_identical_coordinates_coalesce_without_overwriting_evidence():
    index, issues = citydata_geometry_index(
        [source([row()], "a"), source([row("127.0_37.50|127.01_37.5")], "b")]
    )
    assert not issues and index["1"]["state"] == "valid"
    assert len(index["1"]["geometry_hashes"]) == 1
    assert {s["payload_id"] for s in index["1"]["sources"]} == {"a", "b"}


@pytest.mark.parametrize("other", ["127_37.5|127.02_37.5", "127.01_37.5|127_37.5"])
def test_conflicting_geometry_including_reversed_order_is_not_selected_by_input_order(other):
    inputs = [source([row()], "a"), source([row(other)], "b")]
    first, _ = citydata_geometry_index(inputs)
    second, _ = citydata_geometry_index(list(reversed(inputs)))
    assert first == second
    assert first["1"]["state"] == "conflict" and first["1"]["geometry"] is None
    assert len(first["1"]["geometry_hashes"]) == 2


def test_invalid_variant_does_not_silently_disappear_behind_a_valid_one():
    index, issues = citydata_geometry_index([source([row(), row("NaN_37|127_37.5")])])
    assert not issues
    assert index["1"]["state"] == "invalid" and index["1"]["geometry"] is None


def test_consecutive_duplicate_vertices_do_not_create_a_coordinate_conflict():
    index, _ = citydata_geometry_index([source([row(), row("127_37.5|127_37.5|127.01_37.5")])])
    assert index["1"]["state"] == "valid"


def test_absence_correspondence_and_conflict_have_distinct_outcomes():
    assert validation_outcome([], {}, 5000, set(), [])["error_code"] == "missing_spatial_reference"
    index, issues = citydata_geometry_index([source([row()])])
    missing = validation_outcome([], index, 5000, {"different"}, issues)
    assert missing["error_code"] == "missing_spatial_correspondence"
    assert missing["unmapped_standard_ids"] == ["1"]
    index, _ = citydata_geometry_index([source([row(), row("127_37.5|127.02_37.5")])])
    conflict = validation_outcome([], index, 5000, {"1"}, [])
    assert conflict["error_code"] == "conflicting_spatial_reference"


def test_mismatches_and_duplicate_shapes_do_not_inflate_evidence():
    index, _ = citydata_geometry_index([source([row()])])
    index = {str(i): deepcopy(index["1"]) for i in range(12)}
    pairs = [
        {"service_link_id": str(i), "standard_link_id": str(i), "status": "matched"}
        for i in range(12)
    ]
    report = validation_outcome(pairs, index, 5000, set(index), [])
    assert (
        report["error_code"] == "insufficient_spatial_reference"
        and report["reference_geometry_count"] == 1
    )
    pairs[-1]["status"] = "crossing_or_touch_only"
    report = validation_outcome(pairs, index, 5000, set(index), [])
    assert report["error_code"] == "insufficient_spatial_reference"
    assert report["quarantined_service_ids"] == ["11"]
    assert not report["all_roads_verified"] and not report["direction_verified"]


def test_partial_extent_minority_is_reported_without_blocking_official_geometry():
    index = {
        str(i): {"state": "valid", "geometry": None, "geometry_hashes": [str(i)], "issues": []}
        for i in range(13)
    }
    pairs = [
        {"service_link_id": str(i), "standard_link_id": str(i), "status": "matched"}
        for i in range(12)
    ]
    pairs[-1]["status"] = "partial_overlap"
    report = validation_outcome(pairs, index, 5000, set(index) - {"12"}, [])
    assert report["error_code"] is None
    assert report["advisory_pair_count"] == 1
    assert report["pair_status_counts"] == {"matched": 11, "partial_overlap": 1}
    assert report["provider_epsg_confirmed"] and not report["all_roads_verified"]
    assert report["warnings"] == ["partial_overlap_in_official_mapping", "unmapped_standard_links"]
    pairs[-2]["status"] = "partial_overlap"
    assert validation_outcome(pairs, index, 5000, set(index), [])["error_code"] == (
        "road_geometry_mismatch"
    )


def test_localized_mismatch_is_quarantined_but_widespread_mismatch_still_holds_import():
    index = {
        str(i): {"state": "valid", "geometry": None, "geometry_hashes": [str(i)], "issues": []}
        for i in range(12)
    }
    pairs = [
        {"service_link_id": str(i), "standard_link_id": str(i), "status": "matched"}
        for i in range(12)
    ]
    pairs[-1]["status"] = "separated"
    report = validation_outcome(pairs, index, 5000, set(index), [])
    assert report["error_code"] is None
    assert report["quarantined_service_ids"] == ["11"]
    assert report["pair_status_counts"] == {"matched": 11, "separated": 1}
    assert not report["all_roads_verified"]
    pairs[-2]["status"] = "shape_mismatch"
    assert (
        validation_outcome(pairs, index, 5000, set(index), [])["error_code"]
        == "road_geometry_mismatch"
    )
