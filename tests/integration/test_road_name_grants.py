"""Exercise name enrichment under the deployed collector's column privileges."""

from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import RoadLink
from app.repositories.road_reference import update_road_names
from infra.scripts.w4_database import apply_table_grants

pytestmark = pytest.mark.integration


def test_collector_can_only_update_road_name_and_timestamp(normalized_database):
    engine = create_engine(normalized_database)
    username = "road_names_" + uuid4().hex
    user = sql.Identifier(username)
    try:
        with engine.connect() as connection:
            try:
                # Everything, including the unique NOLOGIN role, is rolled back.
                connection.begin()
                conn = connection.connection.driver_connection
                conn.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(user))
                conn.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(user))
                conn.execute(
                    "INSERT INTO data_sources(source_code,display_name,provider_name) "
                    "VALUES ('topis_geometry','test','test')"
                )
                conn.execute("""
                    INSERT INTO road_links(link_id,source,geometry,updated_at)
                    VALUES ('1000000000','topis_geometry',
                        ST_GeomFromText('MULTILINESTRING((127 37.5,127.01 37.5))',4326),
                        '2000-01-01T00:00:00Z')
                """)
                before = conn.execute("SELECT updated_at FROM road_links").fetchone()[0]
                apply_table_grants(conn, "collector", "road_links", username)
                # Reapplying must remove an old, excessive column grant as well.
                conn.execute(sql.SQL("GRANT UPDATE (geometry) ON road_links TO {}").format(user))
                apply_table_grants(conn, "collector", "road_links", username)
                conn.execute(sql.SQL("SET LOCAL ROLE {}").format(user))
                with Session(bind=connection) as session:
                    assert update_road_names(
                        session, [{"link_id": "1000000000", "name": "테스트도로"}]
                    ) == (1, 1, [])
                    road = session.get(RoadLink, "1000000000")
                    assert road.name == "테스트도로"
                    assert road.updated_at > before

                # SELECT FOR UPDATE and both allowed columns were used by the ORM.
                # Every other column, row insertion and deletion remain forbidden.
                denied = [
                    sql.SQL("UPDATE road_links SET {}={} WHERE false").format(
                        sql.Identifier(column.name), sql.Identifier(column.name)
                    )
                    for column in RoadLink.__table__.columns
                    if column.name not in {"name", "updated_at"}
                ] + [
                    sql.SQL("INSERT INTO road_links SELECT * FROM road_links WHERE false"),
                    sql.SQL("DELETE FROM road_links WHERE false"),
                ]
                for statement in denied:
                    with pytest.raises(psycopg.errors.InsufficientPrivilege):
                        with conn.transaction():
                            conn.execute(statement)
            finally:
                connection.rollback()
    finally:
        engine.dispose()
