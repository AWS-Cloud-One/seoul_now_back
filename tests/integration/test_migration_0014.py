"""The parking search index is reversible without modifying published records."""

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("normalized_database", ["0013"], indirect=True)
def test_parking_index_upgrade_and_downgrade_preserve_rows(normalized_database):
    engine = create_engine(normalized_database)
    snapshot = text("""
        SELECT p.parking_id, p.name, ST_AsEWKT(p.location::geometry),
            f.feature_id, f.name, ST_AsEWKT(f.geometry)
        FROM parking_facilities p JOIN map_features f ON f.parking_id = p.parking_id
    """)
    index = text("""
        SELECT indexdef FROM pg_indexes
        WHERE schemaname = 'public' AND indexname = 'idx_map_features_parking_geography'
    """)
    try:
        with engine.begin() as connection:
            connection.execute(
                text("""
                INSERT INTO map_layer_versions (layer, geometry_version, is_ready, coverage)
                VALUES ('parking', 'test-v1', true, 'test')
            """)
            )
            connection.execute(
                text("""
                INSERT INTO parking_facilities (parking_id, name, location, identity_status)
                VALUES (gen_random_uuid(), 'kept',
                    ST_GeogFromText('SRID=4326;POINT(126.978 37.5665)'), 'unverified')
            """)
            )
            connection.execute(
                text("""
                INSERT INTO map_features
                    (layer, feature_id, parking_id, name, identity_status, geometry)
                SELECT 'parking', parking_id::text, parking_id, name, identity_status,
                    location::geometry FROM parking_facilities
            """)
            )
            before = connection.execute(snapshot).all()
            assert before and connection.execute(index).scalar_one_or_none() is None
        config = Config("alembic.ini")
        command.upgrade(config, "0014")
        with engine.connect() as connection:
            definition = connection.execute(index).scalar_one()
            assert "USING gist" in definition and "geography" in definition
            assert connection.execute(snapshot).all() == before
        command.downgrade(config, "0013")
        with engine.connect() as connection:
            assert connection.execute(index).scalar_one_or_none() is None
            assert connection.execute(snapshot).all() == before
    finally:
        engine.dispose()
