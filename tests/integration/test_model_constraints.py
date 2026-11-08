"""Validate active model constraints in a disposable PostGIS database."""

import os
from datetime import UTC, datetime, timedelta
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


@pytest.fixture
def result_snapshot(db):
    """Seed only the model relationships; no recommendation engine or provider calls."""
    trip, result_id, now = uuid4(), uuid4(), datetime.now(UTC)
    db.execute(table("chat_sessions").insert().values(trip_id=trip))
    db.execute(
        table("chat_condition_versions")
        .insert()
        .values(
            trip_id=trip,
            version=0,
            origin_label="서울역",
            origin_location="SRID=4326;POINT(126.97 37.55)",
            origin_supported=True,
            origin_region="seoul",
            headcount=2,
        )
    )
    db.execute(
        table("chat_condition_purposes")
        .insert()
        .values(trip_id=trip, condition_version=0, purpose="cafe", position=0)
    )
    values = dict(
        result_id=result_id,
        trip_id=trip,
        condition_version=0,
        request_id=uuid4(),
        kind="recommendation",
        routes_status="ok",
        explanation_status="fallback",
        created_at=now,
        context_expires_at=now + timedelta(minutes=30),
        context_token_digest="a" * 64,
    )
    db.execute(table("recommendation_results").insert().values(**values))
    return values


def snapshot_card(result):
    return dict(
        result_id=result["result_id"],
        place_id="kakao:place",
        result_kind=result["kind"],
        position=0,
        rank=1,
        place_name="당시 카페",
        purpose="cafe",
        place_location="SRID=4326;POINT(127 37.5)",
        place_freshness="unknown",
        place_collected_at=result["created_at"],
        place_source="kakao_local",
        duration_seconds=600,
        distance_meters=1000,
        route_status="available",
        route_freshness="fresh",
        route_collected_at=result["created_at"],
        route_source="kakao_mobility",
        parking_status="available",
        parking_checked_at=result["created_at"],
        parking_search_radius_meters=300,
        parking_nearest_distance_meters=100,
        parking_total_count=2,
        reason_text="자동차 이동시간 600초",
        reason_source="fallback",
    )


def test_latest_place_and_conditions_do_not_change_historical_card(db, result_snapshot):
    now, trip = result_snapshot["created_at"], result_snapshot["trip_id"]
    db.execute(
        table("places")
        .insert()
        .values(
            place_id="kakao:place",
            name="당시 카페",
            location="SRID=4326;POINT(127 37.5)",
            collected_at=now,
            expires_at=now + timedelta(days=1),
        )
    )
    db.execute(table("recommendation_cards").insert().values(**snapshot_card(result_snapshot)))
    db.execute(
        table("places").update().values(name="갱신된 이름", location="SRID=4326;POINT(127.1 37.6)")
    )
    db.execute(
        table("chat_condition_versions").insert().values(trip_id=trip, version=1, headcount=4)
    )
    db.execute(
        table("chat_sessions")
        .update()
        .where(table("chat_sessions").c.trip_id == trip)
        .values(condition_version=1)
    )
    card = db.execute(sa.select(table("recommendation_cards"))).mappings().one()
    assert card["place_name"] == "당시 카페" and card["duration_seconds"] == 600
    assert card["route_observed_at"] is None and card["route_collected_at"] == now
    assert (
        db.scalar(sa.text("SELECT ST_X(place_location::geometry) FROM recommendation_cards")) == 127
    )
    result = db.execute(sa.select(table("recommendation_results"))).mappings().one()
    assert result["condition_version"] == 0 and result["created_at"] == now
    assert (
        db.scalar(
            sa.select(table("chat_condition_versions").c.headcount).where(
                table("chat_condition_versions").c.trip_id == trip,
                table("chat_condition_versions").c.version == result["condition_version"],
            )
        )
        == 2
    )
    db.execute(
        table("chat_condition_purposes")
        .delete()
        .where(
            table("chat_condition_purposes").c.trip_id == trip,
            table("chat_condition_purposes").c.condition_version == 0,
        )
    )
    rejects(
        db,
        table("chat_condition_versions")
        .delete()
        .where(
            table("chat_condition_versions").c.trip_id == trip,
            table("chat_condition_versions").c.version == 0,
        ),
        "fk_recommendation_results_trip_id",
    )


@pytest.mark.parametrize(
    "patch,constraint",
    [
        ({"context_token_digest": "raw-token"}, "context_digest"),
        ({"context_token_digest": "a" * 64}, "uq_"),
        ({"context_expires_at": datetime(2000, 1, 1, tzinfo=UTC)}, "context_expiry"),
        ({"condition_version": 99}, "fk_"),
        ({"trip_id": uuid4()}, "fk_"),
        ({"kind": "no_candidates"}, "result_kind"),
        ({"routes_status": "failed"}, "result_stages"),
    ],
)
def test_invalid_result_context_is_rejected(db, result_snapshot, patch, constraint):
    values = {**result_snapshot, "result_id": uuid4(), "context_token_digest": "b" * 64, **patch}
    rejects(db, table("recommendation_results").insert().values(**values), constraint)


def test_retry_source_cannot_cross_trip_but_condition_values_are_a_service_check(
    db, result_snapshot
):
    trip, other_trip = result_snapshot["trip_id"], uuid4()
    db.execute(table("chat_sessions").insert().values(trip_id=other_trip))
    db.execute(table("chat_condition_versions").insert().values(trip_id=other_trip, version=0))
    db.execute(table("chat_condition_versions").insert().values(trip_id=trip, version=1))
    retry_id = uuid4()
    values = {
        **result_snapshot,
        "result_id": retry_id,
        "context_token_digest": "b" * 64,
        "source_result_id": result_snapshot["result_id"],
    }
    for patch in ({"trip_id": other_trip}, {"source_result_id": uuid4()}):
        rejects(
            db,
            table("recommendation_results").insert().values(**{**values, **patch}),
            "retry_source",
        )
    rejects(
        db,
        table("recommendation_results").insert().values(**{**values, "source_result_id": retry_id}),
        "retry_not_self",
    )
    # The service checks equal values and a general-candidate source before insertion.
    db.execute(
        table("recommendation_results").insert().values(**{**values, "condition_version": 1})
    )


@pytest.mark.parametrize(
    "patch,constraint",
    [
        ({"position": 3, "rank": 4}, "card_position"),
        ({"rank": None}, "card_kind_facts"),
        ({"rank": 2}, "card_kind_facts"),
        ({"duration_seconds": None}, "card_kind_facts"),
        ({"duration_seconds": -1}, "duration_nonnegative"),
        ({"distance_meters": Decimal("NaN")}, "distance_meters_finite"),
        ({"distance_meters": Decimal("Infinity")}, "distance_meters_finite"),
        ({"place_name": " "}, "place_name_nonblank"),
        ({"place_id": "unverified"}, "place_identity"),
        ({"place_location": "SRID=4326;POINT EMPTY"}, "place_location"),
        ({"parking_total_count": 0}, "available_parking"),
        ({"parking_total_count": None}, "available_parking"),
        ({"parking_nearest_distance_meters": -1}, "parking_nearest_distance_meters_finite"),
    ],
)
def test_invalid_snapshot_facts_are_rejected(db, result_snapshot, patch, constraint):
    rejects(
        db,
        table("recommendation_cards")
        .insert()
        .values(**{**snapshot_card(result_snapshot), **patch}),
        constraint,
    )


def test_cards_have_at_most_three_unique_places_and_positions(db, result_snapshot):
    card = snapshot_card(result_snapshot)
    db.execute(table("recommendation_cards").insert().values(**{**card, "place_id": "kakao:0"}))
    rejects(
        db,
        table("recommendation_cards")
        .insert()
        .values(**{**card, "place_id": "kakao:0", "position": 1, "rank": 2}),
        "pk_",
    )
    db.execute(
        table("recommendation_cards").insert(),
        [{**card, "place_id": f"kakao:{i}", "position": i, "rank": i + 1} for i in range(1, 3)],
    )
    rejects(
        db,
        table("recommendation_cards").insert().values(**{**card, "place_id": "kakao:fourth"}),
        "uq_recommendation_cards_position",
    )


def test_evidence_preserves_values_and_requires_matching_recommended_card(db, result_snapshot):
    card = snapshot_card(result_snapshot)
    db.execute(table("recommendation_cards").insert().values(**card))
    evidence = dict(
        result_id=card["result_id"],
        place_id=card["place_id"],
        position=0,
        result_kind="recommendation",
        type="eta",
        text=card["reason_text"],
        duration_seconds=600,
        distance_meters=1000,
        freshness="fresh",
        source="kakao_mobility",
        collected_at=result_snapshot["created_at"],
    )
    for patch, constraint in [
        ({"type": "population"}, "evidence_value"),
        ({"type": "parking"}, "evidence_value"),
        ({"duration_seconds": None}, "evidence_value"),
        ({"place_id": "kakao:other"}, "fk_"),
        ({"result_kind": "general_candidates"}, "recommended_evidence"),
    ]:
        rejects(
            db,
            table("recommendation_evidence").insert().values(**{**evidence, **patch}),
            constraint,
        )
    db.execute(table("recommendation_evidence").insert().values(**evidence))
    db.execute(
        table("recommendation_evidence")
        .insert()
        .values(
            **{
                **evidence,
                "position": 1,
                "type": "parking",
                "text": "주차장까지 직선거리 100m",
                "duration_seconds": None,
                "distance_meters": 100,
                "source": None,
                "collected_at": None,
                "freshness": "unknown",
            }
        )
    )
    rows = (
        db.execute(
            sa.select(table("recommendation_evidence")).order_by(
                table("recommendation_evidence").c.position
            )
        )
        .mappings()
        .all()
    )
    assert rows[0]["observed_at"] is None and rows[0]["duration_seconds"] == 600
    assert rows[1]["source"] is None and rows[1]["distance_meters"] == 100
    db.execute(table("recommendation_results").delete())
    assert db.scalar(sa.select(sa.func.count()).select_from(table("recommendation_cards"))) == 0
    assert db.scalar(sa.select(sa.func.count()).select_from(table("recommendation_evidence"))) == 0
    assert db.scalar(sa.select(sa.func.count()).select_from(table("chat_sessions"))) == 1


def test_general_candidates_do_not_store_fake_eta_rank_or_recommendation_evidence(
    db, result_snapshot
):
    general = {
        **result_snapshot,
        "result_id": uuid4(),
        "context_token_digest": "b" * 64,
        "kind": "general_candidates",
        "routes_status": "failed",
        "explanation_status": "skipped",
    }
    db.execute(table("recommendation_results").insert().values(**general))
    card = {
        **snapshot_card(general),
        "rank": None,
        "duration_seconds": None,
        "distance_meters": None,
        "route_status": "unavailable",
        "route_freshness": "unknown",
        "route_collected_at": None,
        "route_reason_code": "PROVIDER_ERROR",
        "reason_source": "none",
        "reason_text": "이동시간 미확인",
        "parking_status": "unavailable",
        "parking_search_radius_meters": None,
        "parking_total_count": None,
        "parking_nearest_distance_meters": None,
    }
    rejects(
        db,
        table("recommendation_cards").insert().values(**{**card, "duration_seconds": 0}),
        "card_kind_facts",
    )
    rejects(
        db,
        table("recommendation_cards")
        .insert()
        .values(**{**snapshot_card(result_snapshot), "result_id": general["result_id"]}),
        "fk_",
    )
    db.execute(table("recommendation_cards").insert().values(**card))
    rejects(
        db,
        table("recommendation_evidence")
        .insert()
        .values(
            result_id=general["result_id"],
            place_id=card["place_id"],
            position=0,
            result_kind="recommendation",
            type="eta",
            text="거짓 근거",
            duration_seconds=0,
            distance_meters=0,
            freshness="fresh",
        ),
        "fk_",
    )
