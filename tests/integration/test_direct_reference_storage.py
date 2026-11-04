"""Cutover schema and explicit imports preserve good values without an archive store."""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event, func, inspect, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.clients.seoul import CollectionError
from app.models import (
    Base,
    PedestrianSensor,
    PedestrianSensorLatest,
    PopulationArea,
    RoadLink,
    RoadLinkMapping,
)
from app.modules.data_pipeline.collectors.official_files import read_citydata_areas, read_shapes
from app.modules.data_pipeline.normalizers.road_reference import HEADERS, prepare_reference
from app.modules.data_pipeline.pipelines import official_files, supplemental
from app.modules.data_pipeline.pipelines.sdot import save_sdot
from app.repositories.normalization import NormalizedWriter
from app.repositories.reference import ReferenceRepository
from app.repositories.spatial import SpatialRepository
from tests.fixtures.sdot import collected, location, observation, sensor_file

pytestmark = pytest.mark.integration


@pytest.fixture
def no_archive(monkeypatch):
    from app.repositories.collection import CollectionRepository

    assert not hasattr(CollectionRepository, "archive")


def test_places_validate_list_and_boundaries_before_any_replacement(
    normalized_database,
    no_archive,
):
    from pathlib import Path

    folder = Path(__file__).resolve().parents[2] / "data/reference"
    targets = read_citydata_areas(folder / "seoul_areas.xlsx")
    shapes = read_shapes(folder / "citydata_places_official.zip")

    async def run():
        engine = create_async_engine(normalized_database)
        try:
            result = await official_files.import_place_boundaries(engine, targets, shapes)
            assert result["status"] == "success" and result["records"] == 121
            async with async_sessionmaker(engine)() as session:
                before = dict(
                    (
                        await session.execute(
                            select(PopulationArea.area_code, PopulationArea.source_version)
                        )
                    ).all()
                )
                assert await ReferenceRepository(session).citydata_areas() == targets
            changed = dict(targets)
            changed[next(iter(changed))] = "mismatched name"
            result = await official_files.import_place_boundaries(engine, changed, shapes)
            assert result["error_code"] == "place_reference_name_mismatch"
            async with async_sessionmaker(engine)() as session:
                assert (
                    dict(
                        (
                            await session.execute(
                                select(PopulationArea.area_code, PopulationArea.source_version)
                            )
                        ).all()
                    )
                    == before
                )
                assert await ReferenceRepository(session).citydata_areas() == targets
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_sensor_import_and_observation_are_independent_and_failed_update_keeps_good_count(
    normalized_database,
    no_archive,
    monkeypatch,
):
    async def run():
        engine = create_async_engine(normalized_database)
        statements = []
        event.listen(
            engine.sync_engine,
            "before_cursor_execute",
            lambda conn, cursor, statement, *args: statements.append(statement),
        )
        factory = async_sessionmaker(engine)
        try:
            async with factory.begin() as session:
                await session.execute(
                    text("""
                    INSERT INTO data_sources(source_code,display_name,provider_name)
                    VALUES ('sgis_seoul_boundary','test','test') ON CONFLICT DO NOTHING
                """)
                )
                await session.execute(
                    text("""
                    INSERT INTO region_boundaries
                    (code_system,region_code,name,source,source_version,boundary_version,
                     geometry,processor_version,processed_at)
                    VALUES ('sgis','11','서울','sgis_seoul_boundary','2025','boundary-v1',
                     ST_GeomFromText('MULTIPOLYGON(((126 37,128 37,128 38,126 38,126 37)))',4326),
                     'test',now())
                """)
                )
            started = datetime(2026, 11, 4, tzinfo=UTC)
            file = sensor_file()
            result = await official_files.import_sensor_locations(engine, file, started_at=started)
            assert result["status"] == "success"
            statements.clear()
            await official_files.import_sensor_locations(
                engine,
                file,
                started_at=started + timedelta(seconds=1),
            )
            updates = [s for s in statements if s.startswith("UPDATE pedestrian_sensors")]
            assert updates and all("geometry=" not in s.split("WHERE")[0] for s in updates)
            async with factory() as session:
                reference = await ReferenceRepository(session).sensor_reference()
            async with factory.begin() as session:
                result = await save_sdot(
                    session,
                    reference,
                    collected([observation()]),
                    started_at=started + timedelta(hours=1),
                )
                assert result["storage"]["observations_written"] == 1
            monkeypatch.setattr(
                supplemental.collectors,
                "collect_sdot",
                AsyncMock(side_effect=CollectionError("provider_network_error")),
            )
            result = await supplemental.run_supplemental(engine, None, None, "sdot")
            assert result["status"] == "failed"
            invalid = sensor_file([location(E="NaN")])
            result = await official_files.import_sensor_locations(engine, invalid)
            assert result["status"] == "failed"
            async with factory() as session:
                latest = await session.get(PedestrianSensorLatest, ("sdot_people", "1"))
                sensor = await session.get(PedestrianSensor, ("sdot_people", "1"))
                assert latest.count_10min == 0 and latest.attempt_status == "failed"
                assert latest.received_at == datetime(2026, 11, 4, 1, tzinfo=UTC)
                assert sensor.source_version == file["sha256"] and sensor.is_active
                assert (
                    await session.scalar(select(func.count()).select_from(PedestrianSensorLatest))
                    == 1
                )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_road_geometry_and_mapping_roll_back_together_on_mid_write_failure(
    normalized_database,
    no_archive,
    monkeypatch,
):
    vertices, mappings = [], []
    for index in range(10):
        link = str(1000000000 + index)
        for sequence in (1, 2):
            vertices.append(
                {
                    "A": link,
                    "B": str(sequence),
                    "C": str(200000 + index * 300 + sequence * 100),
                    "D": "450000",
                }
            )
        mappings.append({"A": link, "B": "1", "C": str(2000000000 + index)})
    data = {
        name: {
            "header": HEADERS[name],
            "rows": rows,
            "sha256": "a" * 64,
            "filename": name + ".xlsx",
            "byte_size": 0,
        }
        for name, rows in (("vertices", vertices), ("mapping", mappings))
    }

    async def run():
        engine = create_async_engine(normalized_database)
        try:
            async with async_sessionmaker(engine)() as session:
                lines = await SpatialRepository(session).road_lines(
                    prepare_reference(data)[0], 5181
                )
            standards = {r["A"]: r["C"] for r in mappings}
            inputs = [
                {
                    "area_code": "POI001",
                    "received_at": "2026-11-04T00:00:00Z",
                    "data": {
                        "ROAD_TRAFFIC_STTS": {
                            "ROAD_TRAFFIC_STTS": [
                                {
                                    "LINK_ID": standards[link],
                                    "XYLIST": "|".join(
                                        f"{x}_{y}" for x, y in geometry["coordinates"]
                                    ),
                                }
                                for link, geometry in lines
                            ]
                        }
                    },
                }
            ]
            result = await official_files.import_topis_geometry(
                engine,
                data,
                vertices_version="v1",
                mapping_version="m1",
                road_inputs=inputs,
            )
            assert result["status"] == "success" and result["records"] == 10
            original = NormalizedWriter.upsert_geometry
            calls = 0

            def fail_second(writer, name, values, keys):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise ValueError("reference_write_failed")
                return original(writer, name, values, keys)

            monkeypatch.setattr(NormalizedWriter, "upsert_geometry", fail_second)
            result = await official_files.import_topis_geometry(
                engine,
                data,
                vertices_version="v2",
                mapping_version="m2",
                road_inputs=inputs,
            )
            assert result["status"] == "failed" and calls == 2
            async with async_sessionmaker(engine)() as session:
                versions = list(await session.scalars(select(RoadLink.source_version)))
                assert len(versions) == 10 and set(versions) == {"v1"}
                mapping_versions = list(
                    await session.scalars(select(RoadLinkMapping.source_version))
                )
                assert len(mapping_versions) == 10 and set(mapping_versions) == {"m1"}
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize("normalized_database", ["0011"], indirect=True)
def test_0012_preserves_real_receipt_and_attempt_when_dropping_raw(normalized_database):
    engine = create_engine(normalized_database)
    try:
        with engine.begin() as conn:
            conn.execute(
                text("""
                INSERT INTO data_sources(source_code,display_name,provider_name)
                VALUES ('seoul_citydata','test','test') ON CONFLICT DO NOTHING
            """)
            )
            conn.execute(
                text("""
                INSERT INTO population_areas
                (area_code,name,source,geometry,source_version,processor_version,processed_at)
                VALUES ('POI001','test','seoul_citydata',
                ST_GeomFromText('MULTIPOLYGON(((126 37,128 37,128 38,126 38,126 37)))',4326),
                'v1','test','2026-11-04T00:00:00Z')
            """)
            )
            conn.execute(
                text("""
                INSERT INTO collection_runs(id,source,target_key,started_at,status)
                VALUES ('00000000-0000-0000-0000-000000000001','seoul_citydata','area:POI001',
                        '2026-11-04T00:00:00Z','success')
            """)
            )
            conn.execute(
                text("""
                INSERT INTO raw_payloads
                (payload_id,run_id,source,part_key,payload_kind,format,file_ref,sha256,
                 byte_size,document_type,received_at)
                VALUES ('00000000-0000-0000-0000-000000000002',
                        '00000000-0000-0000-0000-000000000001','seoul_citydata','response',
                        'provider_response','json','unused',repeat('a',64),0,'object',
                        '2026-11-04T00:01:00Z')
            """)
            )
            conn.execute(
                text("""
                INSERT INTO population_latest
                (source,area_code,payload_id,observed_at,processed_at,processor_version,min_people,max_people)
                VALUES ('seoul_citydata','POI001','00000000-0000-0000-0000-000000000002',
                        '2026-11-04T00:00:00Z','2026-11-04T00:02:00Z','test',0,5)
            """)
            )
        config = Config("alembic.ini")

        def fail_at_last_drop(connection, cursor, statement, parameters, context, executemany):
            if "DROP TABLE raw_payloads" in statement:
                raise RuntimeError("cutover rollback probe")

        event.listen(engine.__class__, "before_cursor_execute", fail_at_last_drop)
        try:
            with pytest.raises(RuntimeError, match="cutover rollback probe"):
                command.upgrade(config, "head")
        finally:
            event.remove(engine.__class__, "before_cursor_execute", fail_at_last_drop)
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0011"
            assert "raw_payloads" in inspect(conn).get_table_names()
            assert "received_at" not in {
                col["name"] for col in inspect(conn).get_columns("population_latest")
            }
        command.upgrade(config, "head")
        with engine.connect() as conn:
            row = conn.execute(text("SELECT * FROM population_latest")).mappings().one()
            assert row["min_people"] == 0 and row["max_people"] == 5
            assert row["received_at"] == datetime(2026, 11, 4, 0, 1, tzinfo=UTC)
            assert "payload_id" not in row
            attempt = conn.execute(text("SELECT * FROM population_attempt_latest")).mappings().one()
            assert attempt["status"] == "success" and attempt["observation_updated"]
            assert not {"raw_payloads", "legacy_archives", "payload_file_deletions"} & set(
                inspect(conn).get_table_names()
            )
            for table in Base.metadata.tables.values():
                columns = {col["name"]: col for col in inspect(conn).get_columns(table.name)}
                assert set(columns) == set(table.c.keys())
                assert all(columns[col.name]["nullable"] == col.nullable for col in table.c)
    finally:
        engine.dispose()
