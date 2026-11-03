"""PostGIS geometry construction and queries; no provider requests or field mapping."""

import json

from sqlalchemy import text


class SpatialRepository:
    def __init__(self, session):
        self.session = session

    async def road_lines(self, points, source_srid):
        rows = await self.session.execute(
            text("""
                SELECT link_id, ST_AsGeoJSON(ST_Transform(
                    ST_MakeLine(ST_SetSRID(ST_MakePoint(x,y),:srid) ORDER BY seq),4326))
                FROM jsonb_to_recordset(CAST(:points AS jsonb)) AS p(
                    link_id text,seq int,x float8,y float8)
                GROUP BY link_id ORDER BY link_id
            """),
            {"points": json.dumps(points), "srid": source_srid},
        )
        return [(link, json.loads(geometry)) for link, geometry in rows]

    async def line_distance(self, first, second, metric_srid):
        distance = await self.session.scalar(
            text("""
                SELECT ST_Distance(
                    ST_Transform(ST_SetSRID(ST_GeomFromGeoJSON(:a),4326),:srid),
                    ST_Transform(ST_SetSRID(ST_GeomFromGeoJSON(:b),4326),:srid))
            """),
            {"a": json.dumps(first), "b": json.dumps(second), "srid": metric_srid},
        )
        return float(distance)

    async def polygon_from_rings(self, wkt):
        row = (
            await self.session.execute(
                text(
                    "WITH g AS (SELECT ST_BuildArea(ST_Node("
                    "ST_GeomFromText(:wkt,4326))) AS geom) "
                    "SELECT ST_IsValid(ST_MakeValid(geom)),"
                    "ST_AsGeoJSON(ST_CollectionExtract(ST_MakeValid(geom),3)),"
                    "NOT ST_IsValid(geom) FROM g"
                ),
                {"wkt": wkt},
            )
        ).one()
        if not row[0] or not row[1]:
            raise ValueError("invalid_polygon")
        return json.loads(row[1]), bool(row[2])

    async def transform_wkt(self, wkt, source_srid):
        geometry = await self.session.scalar(
            text("SELECT ST_AsGeoJSON(ST_Transform(ST_GeomFromText(:wkt,:srid),4326))"),
            {"wkt": wkt, "srid": source_srid},
        )
        return json.loads(geometry)

    async def transform_geojson(self, geometry, source_srid):
        converted = await self.session.scalar(
            text(
                "SELECT ST_AsGeoJSON(ST_Transform(ST_SetSRID(ST_GeomFromGeoJSON(:geo),:srid),4326))"
            ),
            {"geo": json.dumps(geometry), "srid": source_srid},
        )
        return json.loads(converted)

    async def covers(self, geometry, longitude, latitude):
        return await self.session.scalar(
            text(
                "SELECT ST_Covers(ST_SetSRID(ST_GeomFromGeoJSON(:geo),4326),"
                "ST_SetSRID(ST_Point(:lng,:lat),4326))"
            ),
            {"geo": json.dumps(geometry), "lng": longitude, "lat": latitude},
        )
