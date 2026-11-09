"""0010 preserves existing scalar facts and makes transactional schema changes."""

import ast
import io
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command

from tests.integration.test_migration_0009 import config
from tests.integration.test_migration_0009 import migration_database as migration_database

pytestmark = pytest.mark.integration


def fail_upgrade_at(marker):
    def fail(connection, cursor, statement, parameters, context, executemany):
        if marker in statement:
            raise RuntimeError("rollback probe")

    sa.event.listen(sa.engine.Engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(RuntimeError, match="rollback probe"):
            command.upgrade(config(), "head")
    finally:
        sa.event.remove(sa.engine.Engine, "before_cursor_execute", fail)


def test_0010_preserves_legacy_hours_ids_and_rolls_back_atomically(migration_database):
    engine = migration_database
    command.upgrade(config(), "0009")
    facility_id, record_id = uuid4(), uuid4()
    with engine.begin() as connection:
        connection.exec_driver_sql("""
            INSERT INTO data_sources (source_code, display_name, provider_name)
            VALUES ('b04_fixture', 'fixture', 'fixture')
        """)
        connection.execute(
            sa.text("""
            INSERT INTO parking_facilities (parking_id, name, location)
            VALUES (:id, 'legacy', ST_GeogFromText('POINT(127 37.5)'))
        """),
            {"id": facility_id},
        )
        connection.execute(
            sa.text("""
            INSERT INTO parking_source_records
                (source_record_id, parking_id, source, legacy_record_id, name,
                 base_minutes, base_fee_krw, weekday_hours, saturday_hours,
                 sunday_holiday_hours, hours_notes)
            VALUES (:id, :fid, 'b04_fixture', 'old-hash', 'legacy', 5, 0,
                    '0000~0000', '0900~1800', 'unknown', '원래 안내')
        """),
            {"id": record_id, "fid": facility_id},
        )
    fail_upgrade_at("DROP COLUMN sunday_holiday_hours")
    assert "weekday_hours" in {
        c["name"] for c in sa.inspect(engine).get_columns("parking_source_records")
    }
    assert "parking_operating_hours" not in sa.inspect(engine).get_table_names()
    command.upgrade(config(), "head")
    command.upgrade(config(), "head")
    command.check(config())
    with engine.connect() as connection:
        row = connection.execute(sa.text("SELECT * FROM parking_source_records")).mappings().one()
        assert (row["source_record_id"], row["parking_id"], row["legacy_record_id"]) == (
            record_id,
            facility_id,
            "old-hash",
        )
        assert row["base_fee_krw"] == 0 and row["base_usable"] and not row["additional_usable"]
        assert row["hours_notes"] == "원래 안내" and row["is_paid"] is None
        hours = (
            connection.execute(sa.text("SELECT * FROM parking_operating_hours")).mappings().all()
        )
        assert {h["source_text"] for h in hours} == {"0000~0000", "0900~1800", "unknown"}
        assert all(
            h["state"] == "unknown"
            and h["reason_code"] == "LEGACY_HOURS_UNVERIFIED"
            and h["opens_at"] is None
            for h in hours
        )
        assert (
            connection.exec_driver_sql("SELECT count(*) FROM parking_availability_latest").scalar()
            == 0
        )
        assert (
            connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar() == "0013"
        )


def test_0010_preserves_road_keys_without_guessing_sequence(migration_database):
    engine = migration_database
    command.upgrade(config(), "0009")
    with engine.begin() as c:
        c.exec_driver_sql("""INSERT INTO data_sources(source_code,display_name,provider_name)
            VALUES ('topis_geometry','roads','fixture'),
                ('topis_standard_mapping','mapping','fixture')""")
        c.exec_driver_sql("""INSERT INTO road_links(link_id,source,geometry)
            VALUES ('a','topis_geometry',
                ST_GeomFromText('MULTILINESTRING((127 37.5,127.01 37.5))',4326))""")
        c.exec_driver_sql("""INSERT INTO road_link_mappings
                (service_link_id,sequence,standard_link_id,source,source_version)
            VALUES ('a',0,'x','topis_standard_mapping','2025'),
                ('a',1,'y','topis_standard_mapping','2025')""")
    fail_upgrade_at("DROP COLUMN sunday_holiday_hours")
    assert "sequence_status" not in {
        c["name"] for c in sa.inspect(engine).get_columns("road_link_mappings")
    }
    command.upgrade(config(), "head")
    command.upgrade(config(), "head")
    command.check(config())
    with engine.connect() as c:
        assert c.exec_driver_sql(
            "SELECT sequence,standard_link_id,sequence_status "
            "FROM road_link_mappings ORDER BY sequence"
        ).all() == [(0, "x", "unknown"), (1, "y", "unknown")]


def test_0010_is_fixed_and_requires_online_preservation_checks():
    path = Path(__file__).resolve().parents[2] / "migrations/versions/0010_pipeline_storage.py"
    module = ast.parse(path.read_text())
    imports = [node.module for node in ast.walk(module) if isinstance(node, ast.ImportFrom)]
    assert not any(name and (name == "app" or name.startswith("app.")) for name in imports)
    with pytest.raises(RuntimeError, match="requires online"):
        command.upgrade(config(output_buffer=io.StringIO()), "0009:0010", sql=True)
