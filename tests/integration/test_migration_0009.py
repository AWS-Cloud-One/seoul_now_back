"""Replay both historical 0008 variants directly into the final 0009 schema."""

import io
import json
import os
import runpy
import shutil
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.engine import make_url

from app.models import Base
from app.storage.payloads import configured_store

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
LEGACY_MAP = ROOT / "tests/fixtures/migrations/legacy_0008_map_contract.py"


@pytest.fixture
def migration_database(monkeypatch):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to an isolated PostGIS test server with CREATEDB")
    database = "migration_0009_" + uuid4().hex
    admin = sa.create_engine(url, isolation_level="AUTOCOMMIT")
    created = False
    engine = None
    try:
        with admin.connect() as connection:
            connection.exec_driver_sql(f'CREATE DATABASE "{database}" TEMPLATE template0')
            created = True
        test_url = make_url(url).set(database=database)
        monkeypatch.setenv("APP_DATABASE_URL", test_url.render_as_string(hide_password=False))
        engine = sa.create_engine(test_url)
        yield engine
    finally:
        if engine is not None:
            engine.dispose()
        if created:
            with admin.connect() as connection:
                connection.exec_driver_sql(f'DROP DATABASE "{database}" WITH (FORCE)')
        admin.dispose()


def config(script_location=None, output_buffer=None):
    result = Config(str(ROOT / "alembic.ini"), output_buffer=output_buffer)
    result.set_main_option("script_location", str(script_location or ROOT / "migrations"))
    return result


def prepare_history(engine, history, tmp_path):
    if history == "fresh":
        return
    if history == "map_0008":
        # Keep the original map migration as an immutable fixture, so this test
        # really runs the old branch's chain rather than stamping the new schema.
        scripts = tmp_path / "legacy_migrations"
        shutil.copytree(ROOT / "migrations", scripts, ignore=shutil.ignore_patterns("__pycache__"))
        (scripts / "versions/0008_collection_history.py").unlink()
        (scripts / "versions/0009_relational_storage.py").unlink()
        shutil.copyfile(LEGACY_MAP, scripts / "versions/0008_map_contract.py")
        command.upgrade(config(scripts), "0008")
    else:
        command.upgrade(config(), "0007" if history == "0007" else "0008")
        if history == "both_0008":
            with engine.begin() as connection:
                context = MigrationContext.configure(
                    connection, opts={"target_metadata": Base.metadata}
                )
                with Operations.context(context):
                    runpy.run_path(str(LEGACY_MAP))["upgrade"]()


def seed_existing_data(engine):
    run_id, response_id, observation_id, parking_id, source_id = (uuid4() for _ in range(5))
    with engine.begin() as connection:
        tables = set(sa.inspect(connection).get_table_names())
        if "collection_runs" not in tables:
            return {}
        connection.execute(
            sa.text("""
            INSERT INTO collection_runs
                (id, source, started_at, finished_at, status, received, accepted,
                 rejected, duplicates, rejection_reasons)
            VALUES (:id, 'migration_fixture', now(), now(), 'success', 1, 1, 0, 0, '{}')
        """),
            {"id": run_id},
        )
        connection.execute(
            sa.text("""
            INSERT INTO parking_records
                (id, source, source_id, name, location, details, collected_at)
            VALUES ('legacy-parking', 'migration_fixture', '1', '기존 주차장',
                    ST_SetSRID(ST_MakePoint(127, 37.5), 4326)::geography, '{}', now())
        """)
        )
        if "external_api_responses" in tables:
            connection.execute(
                sa.text("""
                INSERT INTO external_api_responses
                    (id, run_id, source, operation, collected_at, payload)
                VALUES (:id, :run_id, 'migration_fixture', 'sample', now(),
                        jsonb_build_object('kept', true))
            """),
                {"id": response_id, "run_id": run_id},
            )
            connection.execute(
                sa.text("""
                INSERT INTO external_api_observations (id, response_id, record_key, data)
                VALUES (:id, :response_id, 'legacy-observation', jsonb_build_object('speed', 25))
            """),
                {"id": observation_id, "response_id": response_id},
            )
        if "map_layer_versions" in tables:
            connection.exec_driver_sql("""
                INSERT INTO map_layer_versions VALUES ('roads', 'legacy-version', true, 'sample');
                INSERT INTO map_features VALUES
                    ('roads', 'legacy-feature',
                     ST_GeomFromText('LINESTRING(127 37.5,127.1 37.6)',4326),
                     '{"name":"기존 도로"}', true);
                INSERT INTO map_observations VALUES
                    ('roads', 'legacy-feature', '{"speed_kmh":0,"travel_seconds":12}',
                     now(), now(), false);
            """)
            connection.execute(
                sa.text("""
                INSERT INTO parking_facilities VALUES (:id, 'unverified', true)
            """),
                {"id": parking_id},
            )
            connection.execute(
                sa.text("""
                INSERT INTO parking_source_records
                    (id, parking_id, legacy_record_id, source, registered, is_active)
                VALUES (:id, :parking_id, 'legacy-parking', 'migration_fixture', '{}', true)
            """),
                {"id": source_id, "parking_id": parking_id},
            )
        return snapshot(connection)


def snapshot(connection):
    result = {}
    quote = connection.dialect.identifier_preparer.quote
    for name in sa.inspect(connection).get_table_names(schema="public"):
        if name in {"alembic_version", "spatial_ref_sys"}:
            continue
        columns = sa.inspect(connection).get_columns(name, schema="public")
        projection = ",".join(f"{quote(c['name'])}::text" for c in columns)
        result[name] = [
            list(row)
            for row in connection.exec_driver_sql(f"SELECT {projection} FROM public.{quote(name)}")
        ]
    return result


@pytest.mark.parametrize("history", ["fresh", "0007", "collection_0008", "map_0008", "both_0008"])
def test_upgrade_converges_and_preserves_existing_rows(migration_database, tmp_path, history):
    engine = migration_database
    prepare_history(engine, history, tmp_path)
    before = seed_existing_data(engine)
    command.upgrade(config(), "head")
    command.upgrade(config(), "head")
    command.check(config())
    with engine.connect() as c:
        assert c.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == "0009"
        inspector = sa.inspect(c)
        assert set(inspector.get_table_names(schema="public")) - {
            "alembic_version",
            "spatial_ref_sys",
        } == set(Base.metadata.tables)
        assert not any(s.startswith("legacy_v") for s in inspector.get_schema_names())
        assert (
            c.exec_driver_sql("""SELECT count(*) FROM information_schema.columns
            WHERE table_schema='public' AND udt_name IN ('json','jsonb')""").scalar_one()
            == 0
        )
        expected = 11 if history == "map_0008" else 13 if history == "both_0008" else 8
        archives = c.exec_driver_sql("SELECT * FROM legacy_archives").mappings().all()
        assert len(archives) == expected
        assert set(before) <= {row.table_name for row in archives}
        store = configured_store()
        for row in archives:
            content = store.read_bytes(row.file_ref, row.sha256, row.byte_size)
            lines = [json.loads(line) for line in content.splitlines()]
            assert row.source_revision == "0008" and lines[0]["source_revision"] == "0008"
            assert lines[0]["table"] == row.table_name and lines[0]["ddl"]
            assert len(lines) - 1 == row.row_count
            if row.table_name in before:
                assert sorted(map(repr, lines[1:])) == sorted(map(repr, before[row.table_name]))
        if history in {"map_0008", "both_0008"}:
            assert (
                c.exec_driver_sql("SELECT feature_id FROM map_features").scalar_one()
                == "legacy-feature"
            )
            assert (
                c.exec_driver_sql("SELECT speed_kmh FROM map_road_observations").scalar_one() == 0
            )
        assert any(
            index["dialect_options"].get("postgresql_using") == "gist"
            for index in inspector.get_indexes("map_features")
        )
    with pytest.raises(RuntimeError, match="restoring the DB"):
        command.downgrade(config(), "0008")


def test_offline_upgrade_requires_the_existing_database():
    output = io.StringIO()
    with pytest.raises(RuntimeError, match="requires online"):
        command.upgrade(config(output_buffer=output), "0008:0009", sql=True)


def test_legacy_snapshots_normalize_directly_and_record_missing_geometry(migration_database):
    engine = migration_database
    command.upgrade(config(), "0008")
    boundary = {
        "features": [
            {
                "id": "POI001",
                "properties": {"name": "검증 장소"},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[127, 37], [128, 37], [128, 38], [127, 38], [127, 37]]],
                },
            }
        ]
    }
    roads = {
        "features": [
            {
                "id": "LINK1",
                "properties": {"name": "검증 도로"},
                "geometry": {"type": "LineString", "coordinates": [[127, 37.5], [127.1, 37.6]]},
            }
        ],
        "mapping_rows": [],
    }
    with engine.begin() as c:
        for source, data in [
            ("citydata_place_boundaries", boundary),
            ("topis_geometry:2025", roads),
            ("topis:LINK1", {"rows": [{"link_id": "LINK1", "prcs_spd": "0"}]}),
        ]:
            c.execute(
                sa.text("""INSERT INTO provider_snapshots(source,data,collected_at)
                VALUES(:source,CAST(:data AS jsonb),now())"""),
                {"source": source, "data": json.dumps(data)},
            )
        for area in ["POI001", "NO_GEOMETRY"]:
            data = {
                "AREA_CD": area,
                "LIVE_PPLTN_STTS": [
                    {
                        "AREA_PPLTN_MIN": "0",
                        "AREA_PPLTN_MAX": "0",
                        "AREA_CONGEST_LVL": "여유",
                        "REPLACE_YN": "N",
                        "PPLTN_TIME": "2026-11-01T09:00:00+09:00",
                    }
                ],
            }
            c.execute(
                sa.text("""INSERT INTO citydata_snapshots
                (area_code,area_name,data,collected_at)
                VALUES(:area,'검증 장소',CAST(:data AS jsonb),now())"""),
                {"area": area, "data": json.dumps(data)},
            )
    command.upgrade(config(), "0009")
    with engine.connect() as c:
        assert c.exec_driver_sql("SELECT speed_kmh FROM road_traffic_latest").scalar_one() == 0
        assert c.exec_driver_sql(
            "SELECT min_people,max_people,is_replacement FROM population_latest"
        ).one() == (0, 0, False)
        assert c.exec_driver_sql("""SELECT r.status,e.reason_code,e.occurrence_count
            FROM collection_runs r JOIN collection_rejection_reasons e ON e.run_id=r.id
            WHERE r.target_key='area:NO_GEOMETRY'""").one() == (
            "partial",
            "missing_population_geometry",
            1,
        )
        assert (
            c.exec_driver_sql("""SELECT count(*) FROM raw_payloads
            WHERE legacy_table='citydata_snapshots'""").scalar_one()
            == 2
        )


def test_existing_parking_ids_zero_fees_and_source_time_are_preserved(migration_database, tmp_path):
    engine = migration_database
    prepare_history(engine, "both_0008", tmp_path)
    parking, record, run, response = (uuid4() for _ in range(4))
    with engine.begin() as c:
        c.execute(
            sa.text("""INSERT INTO collection_runs
            (id,source,started_at,finished_at,status,received,accepted,
             rejected,duplicates,rejection_reasons)
            VALUES (:id,'seoul_getparkinfo','2026-10-20T01:00Z',
                    '2026-10-20T01:01Z','success',1,1,0,0,'{}')"""),
            {"id": run},
        )
        c.execute(
            sa.text("""INSERT INTO external_api_responses
            (id,run_id,source,operation,collected_at,payload)
            VALUES (:id,:run,'seoul_getparkinfo','GetParkInfo',
                    '2026-10-20T01:00Z','{"raw": true}')"""),
            {"id": response, "run": run},
        )
        c.execute(
            sa.text("""INSERT INTO parking_records
            (id,source,source_id,name,address,location,details,collected_at)
            VALUES ('old-hash','seoul_getparkinfo','001','원본 주차장','서울',
            ST_SetSRID(ST_MakePoint(127,37.5),4326)::geography,'{"TPKCT":"0","PRK_CRG":"0"}','2026-10-20T01:00Z')""")
        )
        c.execute(
            sa.text("INSERT INTO parking_facilities VALUES(:id,'verified',true)"), {"id": parking}
        )
        c.execute(
            sa.text("""INSERT INTO parking_source_records
            (id,parking_id,legacy_record_id,source,registered,is_active)
            VALUES(:id,:pid,'old-hash','seoul_getparkinfo','{}',true)"""),
            {"id": record, "pid": parking},
        )
        c.exec_driver_sql(
            "INSERT INTO map_layer_versions VALUES ('parking','old-version',true,'coverage')"
        )
        c.execute(
            sa.text("""INSERT INTO map_features VALUES('parking',:id,
            ST_SetSRID(ST_MakePoint(127,37.5),4326),CAST(:props AS jsonb),true)"""),
            {
                "id": str(parking),
                "props": json.dumps(
                    {
                        "name": "수동 확인 주차장",
                        "parking_id": str(parking),
                        "identity_status": "verified",
                    }
                ),
            },
        )
    command.upgrade(config(), "0009")
    command.check(config())
    with engine.connect() as c:
        f = (
            c.execute(
                sa.text("SELECT * FROM parking_facilities WHERE parking_id=:id"), {"id": parking}
            )
            .mappings()
            .one()
        )
        assert f["identity_status"] == "verified" and f["name"] == "수동 확인 주차장"
        r = (
            c.execute(
                sa.text("SELECT * FROM parking_source_records WHERE source_record_id=:id"),
                {"id": record},
            )
            .mappings()
            .one()
        )
        assert (
            r["parking_id"] == parking and r["registered_capacity"] == 0 and r["base_fee_krw"] == 0
        )
        assert r["collected_at"] is None and r["source_updated_at"] is None
        raw = (
            c.execute(sa.text("SELECT * FROM raw_payloads WHERE payload_id=:id"), {"id": response})
            .mappings()
            .one()
        )
        assert raw["received_at"] is None and raw["legacy_time_basis"] == "run_started"
        assert (
            c.exec_driver_sql(
                "SELECT is_ready FROM map_layer_versions WHERE layer='parking'"
            ).scalar()
            is True
        )
        assert c.exec_driver_sql(
            "SELECT feature_id FROM map_features WHERE layer='parking'"
        ).scalar() == str(parking)


def seed_legacy_evidence(engine):
    command.upgrade(config(), "0008")
    run_id, json_id, child_id = uuid4(), uuid4(), uuid4()
    with engine.begin() as c:
        c.execute(
            sa.text("""INSERT INTO collection_runs
            (id,source,started_at,status,received,accepted,rejected,duplicates,rejection_reasons)
            VALUES(:id,'fixture',now(),'partial',4,1,3,0,
                   '{"missing": 2,"invalid": 1,"zero": 0}')"""),
            {"id": run_id},
        )
        c.execute(
            sa.text("""INSERT INTO external_api_responses
            (id,run_id,source,operation,collected_at,payload)
            VALUES(:id,:run,'fixture','sample','2026-11-01T00:00:00Z',
            '{"precision": 12345678901234567890.123456789,
              "zero": 0,"no": false,"unknown": null}')"""),
            {"id": json_id, "run": run_id},
        )
        c.execute(
            sa.text("""INSERT INTO external_api_observations
            (id,response_id,record_key,data)
            VALUES(:id,:parent,'child','{"xml":"<서울>0</서울>"}')"""),
            {"id": child_id, "parent": json_id},
        )
        c.exec_driver_sql("""INSERT INTO provider_snapshots(source,data,collected_at)
            VALUES('unrecognized','{"number": 12345678901234567890.123456789}',now())""")
        before = c.execute(
            sa.text("SELECT payload::text FROM external_api_responses WHERE id=:id"),
            {"id": json_id},
        ).scalar_one()
    return run_id, json_id, child_id, before


def test_direct_export_preserves_precision_identity_parent_and_rejection_counts(migration_database):
    engine = migration_database
    run_id, json_id, child_id, before = seed_legacy_evidence(engine)
    command.upgrade(config(), "0009")
    store = configured_store()
    with engine.connect() as c:
        raw = (
            c.execute(sa.text("SELECT * FROM raw_payloads WHERE payload_id=:id"), {"id": json_id})
            .mappings()
            .one()
        )
        assert store.read_bytes(raw.file_ref, raw.sha256, raw.byte_size).decode() == before
        assert raw.received_at is None and raw.legacy_time_basis == "run_started"
        assert raw.legacy_collected_at.isoformat() == "2026-11-01T00:00:00+00:00"
        child = (
            c.execute(
                sa.text("SELECT * FROM raw_payloads WHERE legacy_key=:id"), {"id": str(child_id)}
            )
            .mappings()
            .one()
        )
        assert child.parent_payload_id == json_id and child.run_id == run_id
        assert store.load_json(SimpleNamespace(**dict(child))) == {"xml": "<서울>0</서울>"}
        assert set(
            c.exec_driver_sql(
                "SELECT reason_code,occurrence_count FROM collection_rejection_reasons"
            )
        ) == {("missing", 2), ("invalid", 1)}
        assert store.load_json(SimpleNamespace(**dict(raw)))["zero"] == 0
        archived = (
            c.exec_driver_sql("SELECT * FROM legacy_archives WHERE table_name='provider_snapshots'")
            .mappings()
            .one()
        )
        rows = [
            json.loads(line)
            for line in store.read_bytes(
                archived.file_ref, archived.sha256, archived.byte_size
            ).splitlines()
        ]
        row = dict(zip([column["name"] for column in rows[0]["columns"]], rows[1], strict=True))
        assert row["data"] == '{"number": 12345678901234567890.123456789}'


def test_failed_file_export_rolls_back_schema_data_and_version(
    migration_database, monkeypatch, tmp_path
):
    engine = migration_database
    _, json_id, _, before = seed_legacy_evidence(engine)
    blocked_root = tmp_path / "not-a-directory"
    blocked_root.write_text("keep this file")
    monkeypatch.setenv("APP_PAYLOAD_STORAGE_DIR", str(blocked_root))
    with pytest.raises(Exception, match="원본 보관 파일을 저장할 수 없습니다"):
        command.upgrade(config(), "0009")
    with engine.connect() as c:
        assert c.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == "0008"
        assert "legacy_v0008" not in sa.inspect(c).get_schema_names()
        assert "raw_payloads" not in sa.inspect(c).get_table_names()
        assert (
            c.execute(
                sa.text("SELECT payload::text FROM external_api_responses WHERE id=:id"),
                {"id": json_id},
            ).scalar_one()
            == before
        )
        assert c.exec_driver_sql("SELECT rejection_reasons FROM collection_runs").scalar_one() == {
            "missing": 2,
            "invalid": 1,
            "zero": 0,
        }
    assert blocked_root.read_text() == "keep this file"


@pytest.mark.parametrize("reasons", ['{"invalid": -1}', '{"invalid": true}', "[]"])
def test_invalid_legacy_counts_abort_without_partial_conversion(migration_database, reasons):
    engine = migration_database
    run_id, _, _, _ = seed_legacy_evidence(engine)
    with engine.begin() as c:
        c.execute(
            sa.text(
                "UPDATE collection_runs SET rejection_reasons=CAST(:value AS jsonb) WHERE id=:id"
            ),
            {"value": reasons, "id": run_id},
        )
    with pytest.raises(RuntimeError, match="invalid legacy rejection"):
        command.upgrade(config(), "0009")
    with engine.connect() as c:
        assert c.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == "0008"
        assert "legacy_v0008" not in sa.inspect(c).get_schema_names()
