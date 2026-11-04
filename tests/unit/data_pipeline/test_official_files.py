"""Official file parsing must retain sparse columns, IDs and validation rules."""

import asyncio
from io import BytesIO
from unittest.mock import AsyncMock
from zipfile import ZipFile

import pytest

from app.core.config import Settings
from app.modules.data_pipeline.collectors.official_files import read_xlsx_rows
from app.modules.data_pipeline.normalizers.official_files import (
    citydata_areas,
    complete_vertices,
    road_features,
    topis_mapping,
    topis_vertices,
    validate_place_reference,
)
from jobs import import_place_boundaries, import_topis_geometry


def test_xlsx_preserves_sparse_columns_shared_strings_and_inline_strings():
    data = BytesIO()
    with ZipFile(data, "w") as book:
        book.writestr(
            "xl/sharedStrings.xml",
            "<sst><si><r><t>공식</t></r><r><t>장소</t></r></si></sst>",
        )
        book.writestr(
            "xl/worksheets/sheet1.xml",
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheetData><row r="1"><c r="A1" t="s"><v>0</v></c>'
            '<c r="C1"><v>0</v></c><c r="D1"/>'
            '<c r="F1" t="inlineStr"><is><t>0012345678</t></is></c></row>'
            '<row r="2"/></sheetData></worksheet>',
        )
    assert read_xlsx_rows(data) == [{"A": "공식장소", "C": "0", "F": "0012345678"}, {}]


def vertex(link="1000000000", seq="1", x="200000", y="450000"):
    return {"A": link, "B": seq, "C": x, "D": y}


def test_duplicate_vertices_coalesce_but_sequence_gaps_exclude_entire_link():
    rows = [
        vertex(seq="2", x="200100"),
        vertex(),
        vertex(),
        vertex(link="1000000001"),
        vertex(link="1000000001", seq="3"),
    ]
    points, excluded, count = complete_vertices(topis_vertices(rows))
    assert count == 2 and excluded == ["1000000001"]
    assert [(p["link_id"], p["seq"]) for p in points] == [("1000000000", 1), ("1000000000", 2)]


@pytest.mark.parametrize(
    "rows, error",
    [
        ([vertex(link="123")], "invalid_official_link"),
        ([vertex(seq="1.5")], "invalid_vertex"),
        ([vertex(seq="0")], "invalid_vertex"),
        ([vertex(x="NaN")], "invalid_vertex"),
        ([vertex(), vertex(x="200001")], "conflicting_vertex_sequence"),
        ([vertex()], "incomplete_vertex_sequence"),
    ],
)
def test_invalid_vertices_are_rejected_before_spatial_processing(rows, error):
    with pytest.raises(ValueError, match=error):
        topis_vertices(rows)


def test_mapping_keeps_explicit_ids_order_and_one_to_many_relations():
    rows = [
        {"A": "1000000000", "B": "2", "C": "2000000002"},
        {"A": "1000000000", "B": "1", "C": "2000000001"},
        {"A": "1000000000", "B": "1", "C": "2000000001"},
        {"A": "1000000001", "B": "0", "C": "2000000001"},
    ]
    result = topis_mapping(rows)
    assert [(r["service_link_id"], r["sequence"], r["standard_link_id"]) for r in result] == [
        ("1000000000", 1, "2000000001"),
        ("1000000000", 2, "2000000002"),
        ("1000000001", 0, "2000000001"),
    ]
    with pytest.raises(ValueError, match="invalid_official_mapping"):
        topis_mapping([{"A": "1000000000", "B": "-1", "C": "2000000001"}])


def test_transformed_coordinates_outside_expected_region_are_rejected():
    with pytest.raises(ValueError, match="transformed_geometry_out_of_region"):
        road_features(
            [("1000000000", {"coordinates": [[200000, 450000], [200100, 450100]]})],
            {},
            version="2026",
            source_srid=5181,
        )


@pytest.mark.parametrize("job", [import_topis_geometry, import_place_boundaries])
@pytest.mark.parametrize("status, code", [("success", 0), ("partial", 2), ("failed", 1)])
def test_import_jobs_report_pipeline_outcome_and_dispose_engine(
    monkeypatch, capsys, job, status, code
):
    engine = AsyncMock()
    monkeypatch.setattr(
        job, "Settings", lambda: Settings(_env_file=None, seoul_citydata_api_key="test")
    )
    if job is import_topis_geometry:
        monkeypatch.setattr(job, "read_topis_files", lambda *args: {})
        monkeypatch.setattr(job, "async_sessionmaker", lambda engine: lambda: AsyncMock())
        monkeypatch.setattr(
            job.ReferenceRepository, "citydata_areas", AsyncMock(return_value={"POI001": "test"})
        )
        monkeypatch.setattr(job, "collect_road_reference_inputs", AsyncMock(return_value=[]))
    monkeypatch.setattr(job, "create_engine", lambda settings: engine)
    pipeline_name = (
        "import_topis_geometry" if job is import_topis_geometry else "import_place_boundaries"
    )
    pipeline = AsyncMock(return_value={"status": status, "error_code": None})
    monkeypatch.setattr(job, pipeline_name, pipeline)
    assert asyncio.run(job.execute()) == code
    assert pipeline.call_args.args[0] is engine
    engine.dispose.assert_awaited_once()
    assert f'"status": "{status}"' in capsys.readouterr().out


def test_official_place_file_has_unique_121_codes():
    from pathlib import Path

    from app.modules.data_pipeline.collectors.official_files import read_shapes

    data = read_shapes(
        Path(__file__).resolve().parents[3] / "data/reference/citydata_places_official.zip"
    )
    assert len(data) == 121
    assert len({row[0]["AREA_CD"] for row in data}) == 121
    assert all(ring[0] == ring[-1] for _, rings in data for ring in rings)


def test_replacement_place_lists_can_change_size_but_must_match_boundaries():
    targets = citydata_areas([{"A": "POI001", "B": "장소"}, {"A": "POI002", "B": "추가 장소"}])
    shapes = [({"AREA_CD": code, "AREA_NM": name}, []) for code, name in targets.items()]
    validate_place_reference(targets, shapes)
    with pytest.raises(ValueError, match="place_reference_code_mismatch"):
        validate_place_reference(targets, shapes[:1])
    with pytest.raises(ValueError, match="conflicting_official_area"):
        citydata_areas([{"A": "POI001", "B": "장소"}, {"A": "POI001", "B": "다른 장소"}])
