"""Validate active model constraints in a disposable PostGIS database."""

import os
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url

from app.models import Base

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def model_engine():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to a disposable PostGIS server with CREATEDB")
    database = "model_constraints_" + uuid4().hex
    admin = sa.create_engine(url, isolation_level="AUTOCOMMIT")
    engine, created = None, False
    try:
        with admin.connect() as connection:
            connection.exec_driver_sql(f'CREATE DATABASE "{database}" TEMPLATE template0')
            created = True
        engine = sa.create_engine(make_url(url).set(database=database))
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE EXTENSION postgis")
            Base.metadata.create_all(connection)
        yield engine
    finally:
        if engine is not None:
            engine.dispose()
        if created:
            with admin.connect() as connection:
                connection.exec_driver_sql(f'DROP DATABASE "{database}" WITH (FORCE)')
        admin.dispose()


@pytest.fixture
def db(model_engine):
    with model_engine.connect() as connection:
        transaction = connection.begin()
        try:
            yield connection
        finally:
            transaction.rollback()


def table(name):
    return Base.metadata.tables[name]


@pytest.fixture
def evidence(db):
    now, run_id = datetime.now(UTC), uuid4()
    db.execute(
        table("data_sources")
        .insert()
        .values(source_code="fixture", display_name="Fixture", provider_name="Fixture provider")
    )
    db.execute(
        table("collection_runs")
        .insert()
        .values(id=run_id, source="fixture", target_key="all", status="success", started_at=now)
    )
    db.execute(
        table("road_links")
        .insert()
        .values(
            link_id="001",
            source="fixture",
            geometry="SRID=4326;MULTILINESTRING((126.9 37.5,127.0 37.6))",
        )
    )
    db.execute(
        table("population_areas")
        .insert()
        .values(
            area_code="AREA1",
            name="Area",
            source="fixture",
            geometry="SRID=4326;MULTIPOLYGON(((126 37,128 37,128 39,126 39,126 37),"
            "(126.5 37.5,126.5 38,127 38,127 37.5,126.5 37.5)))",
        )
    )
    return dict(run_id=run_id, now=now)


def rejects(db, statement, constraint):
    with pytest.raises(sa.exc.IntegrityError) as error, db.begin_nested():
        db.execute(statement)
    assert constraint in error.value.orig.diag.constraint_name


@pytest.mark.parametrize("speed", [Decimal("-1"), Decimal("NaN"), Decimal("Infinity")])
def test_invalid_traffic_numbers_are_rejected(db, evidence, speed):
    rejects(
        db,
        table("road_traffic_latest")
        .insert()
        .values(
            source="fixture",
            link_id="001",
            processed_at=evidence["now"],
            processor_version="test",
            speed_kmh=speed,
        ),
        "speed_kmh_finite",
    )


def test_zero_measurement_and_unknown_observed_time_are_preserved(db, evidence):
    rejects(
        db,
        table("road_links").update().values(processor_version="new-rule-without-time"),
        "processing_pair",
    )
    row = (
        db.execute(
            table("road_traffic_latest")
            .insert()
            .values(
                source="fixture",
                link_id="001",
                processed_at=evidence["now"],
                processor_version="test",
                speed_kmh=0,
            )
            .returning(table("road_traffic_latest"))
        )
        .mappings()
        .one()
    )
    assert row["speed_kmh"] == 0 and row["observed_at"] is None


def test_population_range_and_source_relationship(db, evidence):
    values = dict(
        source="fixture",
        area_code="AREA1",
        processed_at=evidence["now"],
        processor_version="test",
    )
    rejects(
        db,
        table("population_latest").insert().values(**values, min_people=10, max_people=9),
        "people_order",
    )
    rejects(
        db,
        table("population_latest")
        .insert()
        .values(**{**values, "area_code": "missing"}, min_people=0),
        "fk_",
    )
    db.execute(table("population_latest").insert().values(**values, min_people=0))


def test_polygon_holes_survive_and_invalid_shapes_fail(db, evidence):
    inside_hole = db.execute(
        sa.text(
            "SELECT ST_Covers(geometry, ST_SetSRID(ST_Point(126.75,37.75),4326)) "
            "FROM population_areas WHERE area_code='AREA1'"
        )
    ).scalar_one()
    assert inside_hole is False
    rejects(
        db,
        table("population_areas")
        .insert()
        .values(
            area_code="bad",
            name="bad",
            source="fixture",
            geometry="SRID=4326;MULTIPOLYGON(((126 37,128 39,126 39,128 37,126 37)))",
        ),
        "valid_polygon",
    )


def test_duplicate_provider_codes_do_not_merge_facilities(db, evidence):
    facility_ids = [uuid4(), uuid4()]
    for facility_id in facility_ids:
        db.execute(
            table("parking_facilities")
            .insert()
            .values(parking_id=facility_id, name="Parking", location="SRID=4326;POINT(127 37.5)")
        )
        db.execute(
            table("parking_source_records")
            .insert()
            .values(
                parking_id=facility_id,
                source="fixture",
                provider_record_id="same-provider-code",
                base_fee_krw=0,
                registered_capacity=0,
            )
        )
    rows = db.execute(sa.select(table("parking_source_records"))).mappings().all()
    assert {r["parking_id"] for r in rows} == set(facility_ids)
    assert all(r["base_fee_krw"] == r["registered_capacity"] == 0 for r in rows)
    rejects(
        db,
        table("parking_source_records")
        .insert()
        .values(parking_id=facility_ids[0], source="fixture", registered_capacity=-1),
        "capacity_nonnegative",
    )


def test_official_mapping_allows_multiple_standard_links_at_same_sequence(db, evidence):
    for standard in ("STD1", "STD2"):
        db.execute(
            table("road_link_mappings")
            .insert()
            .values(
                service_link_id="001",
                sequence=1,
                standard_link_id=standard,
                source="fixture",
                source_version="2025-03-06",
            )
        )
    assert db.scalar(sa.select(sa.func.count()).select_from(table("road_link_mappings"))) == 2


def test_publication_requires_matching_feature_and_evidence(db, evidence):
    rejects(
        db,
        table("map_layer_versions")
        .insert()
        .values(layer="roads", is_ready=True, coverage="fixture"),
        "ready_version",
    )
    db.execute(
        table("map_layer_versions")
        .insert()
        .values(layer="roads", is_ready=True, geometry_version="v1", coverage="fixture")
    )
    feature = dict(
        layer="roads",
        feature_id="001",
        road_link_id="001",
        geometry="SRID=4326;MULTILINESTRING((126.9 37.5,127 37.6))",
    )
    rejects(
        db,
        table("map_features").insert().values(**{**feature, "feature_id": "wrong"}),
        "layer_identity_geometry",
    )
    db.execute(table("map_features").insert().values(**feature))
    rejects(
        db,
        table("map_road_observations")
        .insert()
        .values(link_id="001", has_observation=True, speed_kmh=0),
        "observation_evidence",
    )
    rejects(
        db,
        table("map_road_observations")
        .insert()
        .values(link_id="001", has_observation=False, speed_kmh=0),
        "absent_values",
    )
    db.execute(
        table("map_road_observations")
        .insert()
        .values(
            link_id="001",
            has_observation=True,
            source="fixture",
            processed_at=evidence["now"],
            processor_version="test",
            speed_kmh=None,
        )
    )
    rejects(db, table("map_features").delete(), "fk_")
