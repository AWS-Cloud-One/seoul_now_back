"""PostGIS geometry construction and queries; no provider requests or field mapping."""

import json
import math

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

    async def line_match_metrics(self, service, standard, *, tolerance_m, segment_length_m):
        """Compare the shorter mapped line with the corresponding portion of the longer one.

        Symmetric containment permits a standard link split across service links as well
        as a service link composed of multiple standard links. It does not verify flow direction.
        """
        row = (
            (
                await self.session.execute(
                    text("""
            WITH lines AS (
                SELECT ST_Transform(ST_SetSRID(ST_GeomFromGeoJSON(:service),4326),5181) AS s,
                       ST_Transform(ST_SetSRID(ST_GeomFromGeoJSON(:standard),4326),5181) AS t
            ), lengths AS (
                SELECT *, ST_Length(s) AS sl, ST_Length(t) AS tl FROM lines
            ), chosen AS (
                SELECT *, CASE WHEN tl<=sl THEN t ELSE s END AS q,
                          CASE WHEN tl<=sl THEN s ELSE t END AS target FROM lengths
            ), projected AS (
                SELECT *, CASE WHEN least(sl,tl)>0
                    THEN ST_LineLocatePoint(target,ST_StartPoint(q)) ELSE 0 END AS f0,
                          CASE WHEN least(sl,tl)>0
                    THEN ST_LineLocatePoint(target,ST_EndPoint(q)) ELSE 0 END AS f1
                FROM chosen
            ), segment AS (
                SELECT *, ST_LineSubstring(target,least(f0,f1),greatest(f0,f1)) AS span
                FROM projected
            )
            SELECT sl AS service_length_m, tl AS standard_length_m,
                   ST_IsSimple(s) AND ST_IsSimple(t) AS simple_lines,
                   ST_Distance(s,t) AS minimum_distance_m,
                   f0 AS start_fraction, f1 AS end_fraction,
                   coalesce(ST_Length(span)/nullif(ST_Length(q),0),0) AS span_ratio,
                   coalesce(ST_Length(ST_Intersection(q,ST_Buffer(target,:tolerance)))
                            /nullif(ST_Length(q),0),0) AS coverage_ratio,
                   ST_HausdorffDistance(ST_Segmentize(q,:step),
                                       ST_Segmentize(span,:step)) AS shape_distance_m
            FROM segment
        """),
                    {
                        "service": json.dumps(service),
                        "standard": json.dumps(standard),
                        "tolerance": tolerance_m,
                        "step": segment_length_m,
                    },
                )
            )
            .mappings()
            .one()
        )
        return dict(row)

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

    async def checked_polygon(self, value, source_srid, *, geojson=False):
        """Repair polygon topology only when area is preserved, then validate stored precision."""
        row = (
            (
                await self.session.execute(
                    text("""
                WITH original AS (
                    SELECT CASE WHEN :geojson THEN ST_SetSRID(ST_GeomFromGeoJSON(:value),:srid)
                         ELSE ST_GeomFromText(:value,:srid) END AS g
                ), repaired AS (
                    SELECT g, CASE WHEN ST_IsValid(g) THEN g ELSE ST_MakeValid(g) END AS fixed
                    FROM original
                ), converted AS (
                    SELECT *, ST_CollectionExtract(fixed,3) AS polygon FROM repaired
                ), serialized AS (
                    SELECT *, ST_AsGeoJSON(ST_Transform(polygon,4326),9) AS geometry FROM converted
                )
                SELECT GeometryType(g) AS original_type, ST_IsValid(g) AS original_valid,
                       ST_IsValidReason(g) AS original_reason, ST_Area(g) AS original_area,
                       ST_Area(polygon) AS repaired_area, ST_IsEmpty(polygon) AS empty,
                       ST_IsValid(polygon) AS repaired_valid,
                       ST_IsValid(ST_GeomFromGeoJSON(geometry)) AS serialized_valid, geometry
                FROM serialized
            """),
                    {
                        "value": json.dumps(value) if geojson else value,
                        "srid": source_srid,
                        "geojson": geojson,
                    },
                )
            )
            .mappings()
            .one()
        )
        before, after = row["original_area"], row["repaired_area"]
        ratio = abs(after - before) / before if before and before > 0 else None
        reason = None
        if row["original_type"] not in {"POLYGON", "MULTIPOLYGON"}:
            reason = "non_polygon_geometry"
        elif row["empty"] or not all(math.isfinite(v) and v > 0 for v in (before, after)):
            reason = "empty_or_degenerate_polygon"
        elif not row["repaired_valid"] or not row["serialized_valid"]:
            reason = "invalid_polygon_after_repair"
        elif not row["original_valid"] and ratio > 0.001:
            reason = "polygon_repair_changes_area"
        quality = {
            "status": "rejected" if reason else "valid" if row["original_valid"] else "repaired",
            "reason": reason,
            "original_validity": row["original_reason"],
            "source_srid": source_srid,
            "area_before_m2": before,
            "area_after_m2": after,
            "relative_area_change": ratio,
            "repair_method": None if row["original_valid"] else "ST_MakeValid(linework)",
        }
        return (None if reason else json.loads(row["geometry"])), quality

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
