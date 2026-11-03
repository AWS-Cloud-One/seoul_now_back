"""Official imports preserve topology, mappings and validation before persistence."""

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import CollectionRun, PopulationArea, RawPayload, RoadLink, RoadLinkMapping
from app.modules.data_pipeline.collectors import official_files as collectors
from app.modules.data_pipeline.normalizers.official_files import topis_vertices
from app.modules.data_pipeline.pipelines import snapshots
from app.modules.data_pipeline.pipelines.citydata import run_citydata
from app.modules.data_pipeline.pipelines.official_files import (
    import_place_boundaries,
    import_topis_geometry,
)
from app.modules.data_pipeline.pipelines.snapshots import save_snapshot
from app.repositories.spatial import SpatialRepository

pytestmark = pytest.mark.integration
DATA = Path(__file__).resolve().parents[2] / "data/reference"


def test_official_boundary_archive_imports_all_121_places(normalized_database):
    async def run():
        engine = create_async_engine(normalized_database)
        try:
            result = await import_place_boundaries(engine, DATA / "citydata_places_official.zip")
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
            vertices.append({"A": service, "B": str(seq), "C": str(x), "D": str(y)})
        mapping.append({"A": service, "B": "0", "C": standard})
    mapping.append({"A": "1000000001", "B": "1", "C": "2000000000"})
    return {"vertices": vertices, "mapping": mapping, "source_sha256": "test-sha256"}


@pytest.mark.parametrize("scenario", ["success", "few_samples", "far_lines", "normalize_failure"])
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
                    topis_vertices(data["vertices"]), 5181
                )
                geometry = dict(lines)["1000000000"]
                assert geometry["coordinates"][0][0] == pytest.approx(127, abs=0.00001)
                assert geometry["coordinates"][0][1] == pytest.approx(37.549, abs=0.001)
                offset = 0.01 if scenario == "far_lines" else 0
                xy = "|".join(f"{x + offset}_{y}" for x, y in geometry["coordinates"])
                count = 8 if scenario == "few_samples" else 12
                # A failed refresh must retain this last good geometry and its receipt.
                previous = await save_snapshot(
                    session,
                    "topis_geometry:2025",
                    {"features": [{"id": "1000000000", "geometry": geometry}]},
                )
                assert previous.status == "success"

            async def citydata():
                return {
                    "AREA_CD": "POI001",
                    "AREA_NM": "테스트",
                    "ROAD_TRAFFIC_STTS": {
                        "ROAD_TRAFFIC_STTS": [
                            {"LINK_ID": str(2000000000 + i), "XYLIST": xy} for i in range(count)
                        ]
                    },
                }

            saved = await run_citydata(engine, citydata, "테스트")
            assert saved["status"] == "partial"  # Population geometry is absent.
            if scenario == "normalize_failure":
                original = snapshots.normalize_snapshot

                def fail(session, key, *args, **kwargs):
                    original(session, key, *args, **kwargs)
                    raise ValueError("failure after typed writes")

                monkeypatch.setattr(snapshots, "normalize_snapshot", fail)
            result = await import_topis_geometry(
                engine, Path("vertices.xlsx"), Path("mapping.xlsx")
            )
            async with factory() as session:
                links = list(await session.scalars(select(RoadLink)))
                runs = list(
                    await session.scalars(
                        select(CollectionRun).where(CollectionRun.source == "topis_geometry")
                    )
                )
                if scenario == "success":
                    assert result["status"] == "success"
                    assert result["file_links"] == 13 and result["geometry_links"] == 12
                    assert result["excluded_sequence_gap_links"] == ["1000000012"]
                    assert (
                        result["crs_check_samples"] == 13 and result["median_line_distance_m"] == 0
                    )
                    assert result["one_to_many_standard_links"] == 1
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
                    assert stored["report"]["source_sha256"] == "test-sha256"
                    assert stored["standard_to_services"]["2000000000"] == [
                        "1000000000",
                        "1000000001",
                    ]
                    assert len(runs) == 2 and all(r.status == "success" for r in runs)
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
                            == 1
                        )
                        assert (
                            await session.scalar(select(func.count()).select_from(RoadLinkMapping))
                            == 0
                        )
                    else:
                        assert result["error_code"] == "coordinate_cross_validation_failed"
                        assert len(runs) == 1
                        if scenario == "few_samples":
                            assert result["samples"] == 9
                        else:
                            assert result["median"] > 30
        finally:
            await engine.dispose()

    asyncio.run(run())
