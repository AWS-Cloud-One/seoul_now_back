"""Read-only RDS data quality and real API map-service checks for W4."""

import asyncio
import hashlib
import json
import sys
from collections import Counter

import psycopg
from psycopg import sql
from w4_database import connect, emit

from app.core.config import Settings
from app.db.session import create_engine, read_session_factory
from app.models import Base
from app.repositories.map import MapRepository
from app.repositories.parking_nearby import ParkingNearbyRepository
from app.repositories.recommendation_geography import RecommendationGeographyRepository
from app.schemas.location import Coordinates
from app.schemas.map import MapQuery
from app.services.map import MapService


def snapshot(settings):
    with connect(settings) as conn, conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        counts = {
            name: conn.execute(
                sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(name))
            ).fetchone()[0]
            for name in sorted(Base.metadata.tables)
        }
        invalid = {}
        for table in Base.metadata.tables.values():
            for column in table.columns:
                if column.type.__class__.__name__ not in {"Geometry", "Geography"}:
                    continue
                ident = sql.Identifier(column.name)
                bad = conn.execute(
                    sql.SQL(
                        "SELECT count(*) FROM {} WHERE {} IS NOT NULL AND "
                        "(ST_IsEmpty({}::geometry) OR NOT ST_IsValid({}::geometry) "
                        "OR ST_SRID({}::geometry)<>4326)"
                    ).format(sql.Identifier(table.name), ident, ident, ident, ident)
                ).fetchone()[0]
                invalid[table.name + "." + column.name] = bad
        layers = [
            dict(zip(("layer", "ready", "version", "features"), row, strict=True))
            for row in conn.execute("""
            SELECT v.layer,v.is_ready,v.geometry_version,count(f.feature_id)
            FROM map_layer_versions v LEFT JOIN map_features f
              ON f.layer=v.layer AND f.is_active
            GROUP BY v.layer,v.is_ready,v.geometry_version ORDER BY v.layer
        """)
        ]
        signatures = {}
        for table, key in (
            ("population_areas", "area_code"),
            ("road_links", "link_id"),
            ("pedestrian_sensors", "sensor_code"),
        ):
            rows = conn.execute(
                sql.SQL(
                    "SELECT {},ST_AsEWKB(geometry),source_version,is_active FROM {} ORDER BY {}"
                ).format(sql.Identifier(key), sql.Identifier(table), sql.Identifier(key))
            )
            digest = hashlib.sha256()
            for row in rows:
                digest.update(
                    json.dumps(
                        [str(x) if not isinstance(x, bytes) else x.hex() for x in row]
                    ).encode()
                )
            signatures[table] = digest.hexdigest()
        recent = [
            dict(
                zip(
                    ("source", "status", "error", "received", "accepted", "rejected"),
                    row,
                    strict=True,
                )
            )
            for row in conn.execute("""
            SELECT source,status,error_code,sum(received),sum(accepted),sum(rejected)
            FROM collection_runs GROUP BY source,status,error_code ORDER BY source,status
        """)
        ]
        observation_counts = {}
        for table in ("map_population_observations", "map_road_observations"):
            observation_counts[table] = dict(
                conn.execute(
                    sql.SQL(
                        "SELECT has_observation,count(*) FROM {} GROUP BY has_observation"
                    ).format(sql.Identifier(table))
                ).fetchall()
            )
        attempt_quality = {}
        for table, column in (
            ("parking_availability_latest", "attempt_status"),
            ("pedestrian_sensor_latest", "attempt_status"),
            ("road_traffic_attempt_latest", "status"),
        ):
            attempt_quality[table] = dict(
                conn.execute(
                    sql.SQL("SELECT {},count(*) FROM {} GROUP BY {}").format(
                        sql.Identifier(column), sql.Identifier(table), sql.Identifier(column)
                    )
                ).fetchall()
            )
        parking_sources = dict(
            conn.execute(
                "SELECT source,count(*) FROM parking_source_records GROUP BY source"
            ).fetchall()
        )
        violations = conn.execute("""
            SELECT count(*) FROM road_area_mappings r JOIN administrative_areas a USING(dong_code)
            WHERE NOT a.is_active OR r.boundary_version<>a.boundary_version
              OR r.road_geometry_version IS DISTINCT FROM a.road_mapping_version
        """).fetchone()[0]
        unvalidated = conn.execute("""
            SELECT count(*) FROM pg_constraint c JOIN pg_namespace n ON c.connamespace=n.oid
            WHERE n.nspname='public' AND NOT c.convalidated
        """).fetchone()[0]
        if any(invalid.values()) or violations or unvalidated:
            raise RuntimeError("data_integrity_failed")
        emit(
            "w4_data_snapshot",
            status="pass",
            counts=counts,
            layers=layers,
            invalid_spatial=invalid,
            reference_signatures=signatures,
            collection=recent,
            published_observations=observation_counts,
            attempt_quality=attempt_quality,
            parking_sources=parking_sources,
            stale_road_area_links=violations,
            unvalidated_constraints=unvalidated,
        )
        return layers


async def map_reads(settings, layers):
    engine = create_engine(settings)
    try:
        for layer in layers:
            if not layer["ready"] or not layer["features"]:
                emit("w4_api_map_read", layer=layer["layer"], status="unavailable")
                continue
            # Same API repository, snapshot isolation and Pydantic response validation.
            async with read_session_factory(engine).begin() as session:
                service = MapService(MapRepository(session))
                query = MapQuery(bbox=(126.95, 37.49, 127.1, 37.57), level=8)
                data = await service.geometry(layer["layer"], query)
                report = dict(
                    layer=layer["layer"],
                    status="pass",
                    features=len(data.feature_collection.features),
                    geometry_version=data.geometry_version,
                )
                if layer["layer"] != "parking":
                    observations = await service.status(
                        layer["layer"], query, data.geometry_version
                    )
                    field = "traffic" if layer["layer"] == "roads" else "population"
                    report["observation_status"] = dict(
                        Counter(getattr(item, field).status for item in observations.items)
                    )
                    report["service_status"] = observations.status
                emit("w4_api_map_read", **report)
        async with read_session_factory(engine).begin() as session:
            nearby = ParkingNearbyRepository(session)
            count, distance = await nearby.summarize_radius(
                Coordinates(longitude=126.978, latitude=37.5665), 1000
            )
            geography = RecommendationGeographyRepository(session)
            areas = await geography.areas()
            area_counts = await geography.parking_counts()
            emit(
                "w4_api_data_read",
                status="pass",
                seoul_city_hall_parking_within_1km=count,
                nearest_distance_meters=float(distance) if distance is not None else None,
                recommendation_areas=len(areas),
                areas_with_published_parking=len(area_counts[0]) if area_counts else 0,
            )
    finally:
        await engine.dispose()


def main():
    try:
        settings = Settings()
        layers = snapshot(settings)
        asyncio.run(map_reads(settings, layers))
        return 0
    except Exception as error:
        emit(
            "w4_data_audit_failed",
            error_type=type(error).__name__,
            sqlstate=error.sqlstate if isinstance(error, psycopg.Error) else None,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
