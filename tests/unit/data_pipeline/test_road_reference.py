"""Reference versions, file evidence and replacement eligibility are explicit."""

from copy import deepcopy
from hashlib import sha256
from pathlib import Path

import pytest

from app.modules.data_pipeline.collectors.official_files import read_topis_files
from app.modules.data_pipeline.normalizers.road_reference import (
    prepare_reference,
    reference_error,
    reference_metadata,
)
from jobs.import_topis_geometry import arguments
from tests.fixtures.road_reference import road_candidate

DATA = Path(__file__).resolve().parents[3] / "data/reference"


@pytest.fixture(scope="module")
def official_inputs():
    return read_topis_files(
        DATA / "topis_vertices_2025.xlsx", DATA / "topis_standard_mapping_20250306.xlsx"
    )


def test_file_hashes_and_parsed_rows_are_returned_without_retaining_binary_copies(official_inputs):
    for file in official_inputs.values():
        body = (DATA / file["filename"]).read_bytes()
        assert file["sha256"] == sha256(body).hexdigest()
        assert file["byte_size"] == len(body)
        assert "body" not in file
    assert official_inputs["vertices"]["sha256"] != official_inputs["mapping"]["sha256"]
    points, mappings, report = prepare_reference(official_inputs)
    assert report["file_links"] == 5093 and report["geometry_links"] == 5092
    assert report["excluded_sequence_gap_links"] == ["1000020300"]
    assert report["duplicate_vertex_rows"] == 0
    assert report["blank_vertex_rows"] == 10895
    assert report["ambiguous_mapping_positions"]
    assert len(report["unknown_sequence_rows"]) == 23
    assert len(report["geometry_without_mapping"]) == 20
    assert len(report["unmatched_mapping_rows"]) == 1
    assert len(mappings) == report["mapping_unique_rows"]
    assert len(points) == report["vertices"]


def test_versions_dates_and_hashes_belong_to_each_file(official_inputs):
    meta = reference_metadata(
        official_inputs,
        vertices_version="2026",
        mapping_version="2026-10-30",
        mapping_date="2026-10-30",
    )
    assert meta["inputs"]["vertices"]["reference_date"] is None
    assert meta["inputs"]["mapping"]["reference_date"] == "2026-10-30"
    assert meta["inputs"]["mapping"]["version"] == "2026-10-30"
    other = reference_metadata(
        official_inputs, vertices_version="2026", mapping_version="2026-11-01"
    )
    assert meta["reference_id"] != other["reference_id"]
    with pytest.raises(ValueError):
        reference_metadata(official_inputs, vertices_version="", mapping_version="2026")


@pytest.mark.parametrize("name", ["vertices", "mapping"])
def test_changed_headers_are_not_silently_read_by_old_column_positions(official_inputs, name):
    data = deepcopy(official_inputs)
    data[name]["header"]["A"] = "changed"
    with pytest.raises(ValueError, match="header"):
        prepare_reference(data)


def test_missing_mapping_sequence_preserves_relation_without_claiming_zero_order(official_inputs):
    _, rows, report = prepare_reference(official_inputs)
    missing = [r for r in rows if r["sequence_status"] == "unknown"]
    assert len(missing) == 23
    assert all(r["sequence"] == 0 for r in missing)
    assert all(r["sequence"] > 0 for r in rows if r["sequence_status"] != "unknown")
    assert len(report["unknown_sequence_rows"]) == len(missing)


def test_partial_or_empty_candidates_cannot_replace_a_reference():
    data = road_candidate({"features": [{"id": "a"}]}, complete=False)
    assert reference_error(data) == "incomplete_road_reference"
    assert reference_error(road_candidate({"features": []})) == "empty_or_duplicate_road_reference"


def test_exclusions_are_exact_and_unrelated_mapping_gaps_are_not_covered():
    data = road_candidate(
        {
            "features": [{"id": "a"}],
            "mapping_rows": [{"service_link_id": "b", "sequence": 1, "standard_link_id": "x"}],
        }
    )
    data["report"].update(file_links=2, excluded_sequence_gap_links=["b"])
    assert reference_error(data) == "unreviewed_reference_exclusions"
    data["reference"]["allowed_exclusions"] = ["b"]
    assert reference_error(data) is None
    data["mapping_rows"][0]["service_link_id"] = "c"
    assert reference_error(data) == "unmatched_reference_mapping"


def test_cli_custom_paths_require_versions_and_partial_intent_is_preserved():
    with pytest.raises(SystemExit):
        arguments(["--vertices-file", "new.xlsx"])
    args = arguments(["--vertices-file", "new.xlsx", "--vertices-version", "2026", "--partial"])
    assert args.vertices_version == "2026" and args.partial
    assert args.vertices_date is None


def test_cli_infers_only_bundled_versions_and_pairs_them_with_expected_bytes():
    args = arguments([])
    assert args.vertices_version == "2025" and args.vertices_date is None
    assert args.mapping_version == args.mapping_date == "2025-03-06"
    assert set(args.expected_hashes) == {"vertices", "mapping"}
    custom = arguments(["--vertices-version", "2026", "--mapping-version", "2026-11-01"])
    assert custom.expected_hashes == {} and custom.mapping_date is None
