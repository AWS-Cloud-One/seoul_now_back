"""Retention migration preserves existing facts and decouples only log references."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from test_migration_0009 import config
from test_migration_0009 import migration_database as migration_database
from test_migration_0010 import fail_upgrade_at

pytestmark = pytest.mark.integration


def test_0011_preserves_receipts_and_last_failure_before_pruning(migration_database):
    engine = migration_database
    command.upgrade(config(), "0010")
    good, failed, payload = uuid4(), uuid4(), uuid4()
    receipt = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
    params = {"good": good, "failed": failed, "payload": payload, "receipt": receipt}
    with engine.begin() as c:
        c.exec_driver_sql(
            "INSERT INTO data_sources(source_code,display_name,provider_name) "
            "VALUES ('topis_traffic','traffic','fixture')"
        )
        c.exec_driver_sql(
            "INSERT INTO road_links(link_id,source,geometry) "
            "VALUES ('a','topis_traffic',"
            "ST_GeomFromText('MULTILINESTRING((127 37.5,127.01 37.5))',4326))"
        )
        c.execute(
            sa.text("""INSERT INTO collection_runs
                (id,source,target_key,status,started_at,finished_at,error_code)
            VALUES (:good,'topis_traffic','link:a','success',:receipt,:receipt,NULL),
            (:failed,'topis_traffic','link:a','failed',:receipt + interval '1 minute',
                :receipt + interval '2 minutes','provider_network_error')"""),
            params,
        )
        c.execute(
            sa.text("""INSERT INTO raw_payloads
                (payload_id,run_id,part_key,payload_kind,format,file_ref,
                 sha256,byte_size,document_type,received_at)
            VALUES (:payload,:good,'response','provider_response','json','fixture',
                repeat('a',64),0,'object',:receipt)"""),
            params,
        )
        c.execute(
            sa.text("""INSERT INTO road_traffic_latest
                (source,link_id,payload_id,processed_at,processor_version,speed_kmh)
            VALUES ('topis_traffic','a',:payload,:receipt,'fixture',25)"""),
            params,
        )
        c.exec_driver_sql(
            "INSERT INTO map_layer_versions(layer,coverage) VALUES ('roads','fixture')"
        )
        c.exec_driver_sql(
            "INSERT INTO map_features(layer,feature_id,road_link_id,geometry) "
            "SELECT 'roads','a','a',geometry FROM road_links"
        )
        c.execute(
            sa.text("""INSERT INTO map_road_observations
                (link_id,source,payload_id,has_observation,processed_at,processor_version,
                 last_attempt_id,collection_failed,speed_kmh)
            VALUES ('a','topis_traffic',:payload,true,:receipt,'fixture',:failed,true,25)"""),
            params,
        )
    fail_upgrade_at("CREATE TABLE payload_file_deletions")
    assert "source" not in {col["name"] for col in sa.inspect(engine).get_columns("raw_payloads")}
    command.upgrade(config(), "head")
    command.upgrade(config(), "head")
    command.check(config())
    with engine.begin() as c:
        assert c.exec_driver_sql("SELECT received_at,speed_kmh FROM road_traffic_latest").one() == (
            receipt,
            25,
        )
        assert c.exec_driver_sql(
            "SELECT received_at,speed_kmh FROM map_road_observations"
        ).one() == (receipt, 25)
        state = c.exec_driver_sql("SELECT * FROM road_traffic_attempt_latest").mappings().one()
        assert state["run_id"] == failed and state["status"] == "failed"
        assert state["error_code"] == "provider_network_error" and not state["observation_updated"]
        assert c.exec_driver_sql("SELECT source,run_id FROM raw_payloads").one() == (
            "topis_traffic",
            good,
        )
        c.exec_driver_sql("DELETE FROM collection_runs")
        assert c.exec_driver_sql("SELECT run_id FROM raw_payloads").scalar() is None
        assert c.exec_driver_sql("SELECT run_id FROM road_traffic_attempt_latest").scalar() is None
        assert (
            c.exec_driver_sql("SELECT last_attempt_id FROM map_road_observations").scalar() is None
        )
        assert c.exec_driver_sql(
            "SELECT received_at,speed_kmh FROM map_road_observations"
        ).one() == (receipt, 25)
