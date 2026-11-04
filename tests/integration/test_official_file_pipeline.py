"""Official imports preserve topology, mappings and validation before persistence."""

import asyncio
import hashlib
from pathlib import Path

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import CollectionRun, PopulationArea, RawPayload, RoadLink, RoadLinkMapping
from app.modules.data_pipeline.collectors import official_files as collectors
from app.modules.data_pipeline.normalizers.official_files import topis_vertices
from app.modules.data_pipeline.normalizers.road_reference import HEADERS
from app.modules.data_pipeline.pipelines import snapshots
from app.modules.data_pipeline.pipelines.citydata import run_citydata
from app.modules.data_pipeline.pipelines.official_files import (
    import_place_boundaries,
    import_topis_geometry,
)
from app.repositories.spatial import SpatialRepository
from app.storage.payloads import configured_store
from tests.fixtures.road_reference import save_fixture_snapshot as save_snapshot

pytestmark = pytest.mark.integration
DATA = Path(__file__).resolve().parents[2] / "data/reference"


def test_official_boundary_archive_imports_all_121_places(normalized_database):
    async def run():
        engine = create_async_engine(normalized_database)
        try:
            result = await import_place_boundaries(engine, DATA / "citydata_places_official.zip")
            publication = result.pop("publication")
            assert publication["status"] == "success"
            assert publication["layers"]["population"]["features"] == 121
            assert result == {"status": "success", "error_code": None, "records": 121}
            async with async_sessionmaker(engine)() as session:
                assert await session.scalar(select(func.count()).select_from(PopulationArea)) == 121
                assert await session.scalar(select(func.count()).select_from(CollectionRun)) == 1
                assert await session.scalar(select(func.count()).select_from(RawPayload)) == 1
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_boundary_inner_ring_is_preserved_in_stored_geometry(
    normalized_database, monkeypatch, stored_payload
):
    outer = [(127, 37.5), (127.1, 37.5), (127.1, 37.6), (127, 37.6), (127, 37.5)]
    inner = [(127.02, 37.52), (127.08, 37.52), (127.08, 37.58), (127.02, 37.58), (127.02, 37.52)]
    monkeypatch.setattr(
        collectors,
        "read_shapes",
        lambda path: [
            ({"AREA_CD": "POI001", "AREA_NM": "공원", "CATEGORY": "공원"}, [outer, inner])
        ],
    )

    async def run():
        engine = create_async_engine(normalized_database)
        try:
            result = await import_place_boundaries(engine, Path("unused.zip"))
            assert result["status"] == "success"
            async with async_sessionmaker(engine)() as session:
                covers = (
                    await session.execute(
                        text(
                            "SELECT ST_Covers(geometry,ST_SetSRID(ST_Point(127.01,37.51),4326)),"
                            "ST_Covers(geometry,ST_SetSRID(ST_Point(127.05,37.55),4326)),"
                            "ST_Covers(geometry,ST_SetSRID(ST_Point(127.02,37.55),4326)) "
                            "FROM population_areas WHERE area_code='POI001'"
                        )
                    )
                ).one()
                assert tuple(covers) == (True, False, True)
                _, stored = await stored_payload(session, "citydata_place_boundaries", "all")
                assert stored["features"][0]["properties"]["topology_repaired"] is False
        finally:
            await engine.dispose()

    asyncio.run(run())


def topis_input():
    vertices, mapping = [], []
    for index in range(13):
        service, standard = str(1000000000 + index), str(2000000000 + index)
        for seq, x, y in [(1, 200000, 450000), (3 if index == 12 else 2, 200100, 450100)]:
            vertices.append(
                {"A": service, "B": str(seq), "C": str(x + max(0, index - 1) * 500), "D": str(y)}
            )
        mapping.append({"A": service, "B": "0", "C": standard})
    mapping.append({"A": "1000000001", "B": "1", "C": "2000000000"})
    return {
        name: dict(
            rows=rows,
            header=HEADERS[name],
            body=name.encode(),
            filename=name + ".xlsx",
            sha256=hashlib.sha256(name.encode()).hexdigest(),
            byte_size=len(name),
        )
        for name, rows in (("vertices", vertices), ("mapping", mapping))
    }


@pytest.mark.parametrize(
    "scenario",
    [
        "success",
        "quarantine",
        "few_samples",
        "far_lines",
        "normalize_failure",
        "hash_mismatch",
        "no_reference",
        "conflict",
        "invalid_reference",
        "crossing",
        "spatial_error",
    ],
)
def test_topis_validation_and_outcome_survive_refactor(
    normalized_database, monkeypatch, scenario, stored_payload
):
    data = topis_input()
    monkeypatch.setattr(collectors, "read_topis_files", lambda *args: data)

    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        try:
            async with factory.begin() as session:
                lines = await SpatialRepository(session).road_lines(
                    topis_vertices(data["vertices"]["rows"]), 5181
                )
                geometry = dict(lines)["1000000000"]
                assert geometry["coordinates"][0][0] == pytest.approx(127, abs=0.00001)
                assert geometry["coordinates"][0][1] == pytest.approx(37.549, abs=0.001)
                offset = 0.01 if scenario == "far_lines" else 0
                count = 8 if scenario == "few_samples" else 12
                # A failed refresh must retain this last good geometry and its receipt.
                previous = await save_snapshot(
                    session,
                    "topis_geometry:2025",
                    {
                        "features": [
                            {
                                "id": "1000000011" if scenario == "quarantine" else "1000000000",
                                "geometry": geometry,
                            }
                        ]
                    },
                )
                assert previous.status == "success"

            async def citydata():
                rows = [
                    {
                        "LINK_ID": str(2000000000 + i),
                        "XYLIST": "|".join(
                            f"{x + offset}_{y}"
                            for x, y in dict(lines)[str(1000000000 + i)]["coordinates"]
                        ),
                    }
                    for i in range(count)
                ]
                if scenario == "no_reference":
                    rows = []
                elif scenario == "conflict":
                    rows.append({"LINK_ID": "2000000000", "XYLIST": "127_37.5|127.02_37.5"})
                elif scenario == "invalid_reference":
                    rows[0]["XYLIST"] = "NaN_37.5|127_37.5"
                elif scenario == "crossing":
                    (x0, y0), (x1, y1) = geometry["coordinates"]
                    rows[0]["XYLIST"] = f"{x0}_{y1}|{x1}_{y0}"
                elif scenario == "quarantine":
                    rows[-1]["XYLIST"] = "|".join(
                        f"{x + 0.01}_{y}" for x, y in dict(lines)["1000000011"]["coordinates"]
                    )
                return {
                    "AREA_CD": "POI001",
                    "AREA_NM": "테스트",
                    "ROAD_TRAFFIC_STTS": {"ROAD_TRAFFIC_STTS": rows},
                }

            saved = await run_citydata(engine, citydata, "테스트", expected_code="POI001")
            assert saved["status"] == "partial"  # Population geometry is absent.
            if scenario == "normalize_failure":
                original = snapshots.normalize_snapshot

                def fail(session, key, *args, **kwargs):
                    original(session, key, *args, **kwargs)
                    raise ValueError("failure after typed writes")

                monkeypatch.setattr(snapshots, "normalize_snapshot", fail)
            if scenario == "spatial_error":

                async def fail_spatial(*args, **kwargs):
                    raise ValueError("synthetic spatial failure")

                monkeypatch.setattr(SpatialRepository, "line_match_metrics", fail_spatial)
            result = await import_topis_geometry(
                engine,
                Path("vertices.xlsx"),
                Path("mapping.xlsx"),
                vertices_version="2026",
                mapping_version="2026-11-01",
                allowed_exclusions=["1000000012"],
                expected_hashes={"vertices": "0" * 64} if scenario == "hash_mismatch" else None,
            )
            async with factory() as session:
                links = list(await session.scalars(select(RoadLink)))
                runs = list(
                    await session.scalars(
                        select(CollectionRun).where(CollectionRun.source == "topis_geometry")
                    )
                )
                if scenario == "success":
                    assert result["status"] == "partial"
                    assert result["file_links"] == 13 and result["geometry_links"] == 12
                    assert result["excluded_sequence_gap_links"] == ["1000000012"]
                    spatial = result["spatial_validation"]
                    assert spatial["compared_pairs"] == 13
                    assert spatial["pair_status_counts"] == {"matched": 13}
                    assert spatial["checked_service_links"] == 12
                    assert len(links) == 12
                    mappings = list(await session.scalars(select(RoadLinkMapping)))
                    assert len(mappings) == 13
                    assert {m.sequence for m in mappings if m.service_link_id == "1000000001"} == {
                        0,
                        1,
                    }
                    assert all(m.service_link_id != "1000000012" for m in mappings)
                    _, stored = await stored_payload(
                        session, "topis_geometry", "file:topis_vertices"
                    )
                    assert (
                        stored["reference"]["inputs"]["vertices"]["sha256"]
                        == data["vertices"]["sha256"]
                    )
                    assert all(m.source_version == "2026-11-01" for m in mappings)
                    assert all(r.source_version == "2026" for r in links)
                    assert {
                        r["service_link_id"]
                        for r in stored["mapping_rows"]
                        if r["standard_link_id"] == "2000000000"
                    } == {"1000000000", "1000000001"}
                    assert len(runs) == 2 and {r.status for r in runs} == {"success", "partial"}
                    input_ids = {v["payload_id"] for v in stored["reference"]["inputs"].values()}
                    originals = list(
                        await session.scalars(
                            select(RawPayload).where(RawPayload.payload_kind == "source_file")
                        )
                    )
                    assert len(originals) == 2
                    assert {str(p.payload_id) for p in originals} == input_ids
                    for original_file in originals:
                        assert original_file.received_at is None
                        assert (
                            configured_store().read_bytes(
                                original_file.file_ref,
                                original_file.sha256,
                                original_file.byte_size,
                            )
                            == data[original_file.part_key]["body"]
                        )
                elif scenario == "quarantine":
                    assert result["status"] == "partial" and result["processing"]["updated"], {
                        "error": result["error_code"],
                        "spatial_error": result["spatial_validation"]["error_code"],
                    }
                    assert result["quarantined_service_ids"] == ["1000000011"]
                    assert result["accepted_geometry_links"] == 11
                    assert result["processing"]["accepted"] == 11
                    assert result["processing"]["rejection_reasons"] == {
                        "excluded_vertex_sequence": 1,
                        "quarantined_road_geometry": 1,
                    }
                    assert result["spatial_validation"]["pair_status_counts"] == {
                        "matched": 12,
                        "separated": 1,
                    }
                    assert len(links) == 12
                    rejected = next(r for r in links if r.link_id == "1000000011")
                    assert (
                        not rejected.is_active and rejected.last_payload_id == previous.payload_id
                    )
                    mappings = list(await session.scalars(select(RoadLinkMapping)))
                    assert len(mappings) == 12
                    assert not any(m.service_link_id == "1000000011" for m in mappings)
                    _, stored = await stored_payload(
                        session, "topis_geometry", "file:topis_vertices"
                    )
                    assert stored["report"]["quarantined_features"][0]["id"] == "1000000011"
                    assert (
                        stored["report"]["quarantined_mapping_rows"][0]["service_link_id"]
                        == "1000000011"
                    )
                    assert "1000000011" not in {f["id"] for f in stored["features"]}
                    assert result["publication"]["layers"]["roads"]["features"] == 11
                else:
                    assert result["status"] == "failed"
                    assert len(links) == 1 and links[0].last_payload_id == previous.payload_id
                    if scenario == "normalize_failure":
                        assert result["error_code"] == "normalization_failed"
                        failed = [r for r in runs if r.status == "failed"]
                        assert len(failed) == 1
                        assert (
                            await session.scalar(
                                select(func.count())
                                .select_from(RawPayload)
                                .where(RawPayload.run_id == failed[0].id)
                            )
                            == 4
                        )
                        assert (
                            await session.scalar(select(func.count()).select_from(RoadLinkMapping))
                            == 0
                        )
                    elif scenario == "hash_mismatch":
                        assert result["error_code"] == "reference_file_changed_requires_version"
                        assert len(runs) == 2
                    else:
                        assert len(runs) == 2
                        spatial = result["spatial_validation"]
                        if scenario == "few_samples":
                            assert result["error_code"] == "insufficient_spatial_reference"
                            assert spatial["compared_pairs"] == 9
                        elif scenario in {
                            "no_reference",
                            "conflict",
                            "invalid_reference",
                            "spatial_error",
                        }:
                            assert (
                                result["error_code"]
                                == {
                                    "no_reference": "missing_spatial_reference",
                                    "conflict": "conflicting_spatial_reference",
                                    "invalid_reference": "invalid_spatial_reference",
                                    "spatial_error": "spatial_validation_failed",
                                }[scenario]
                            )
                        else:
                            assert result["error_code"] == "road_geometry_mismatch"
                            if scenario == "far_lines":
                                assert spatial["pair_status_counts"] == {"separated": 13}
                            else:
                                assert spatial["pair_status_counts"]["crossing_or_touch_only"] == 2
                                assert spatial["pair_status_counts"]["matched"] == 11
        finally:
            await engine.dispose()

    asyncio.run(run())
