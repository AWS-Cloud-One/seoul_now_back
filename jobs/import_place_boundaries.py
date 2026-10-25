"""Import official WGS84 121-place SHP, preserving inner rings with ST_BuildArea."""

import asyncio
import json
import struct
import zipfile
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import Settings
from app.db.session import create_engine
from app.repositories.provider import save_snapshot


def read_shapes(path):
    with zipfile.ZipFile(path) as archive:

        def read(suffix):
            return archive.read(next(n for n in archive.namelist() if n.endswith(suffix)))

        if "WGS_1984" not in read(".prj").decode():
            raise ValueError("unexpected_crs")
        dbf, shp = read(".dbf"), read(".shp")
        header, size = struct.unpack_from("<HH", dbf, 8)
        count = struct.unpack_from("<I", dbf, 4)[0]
        fields = [
            (dbf[o : o + 11].split(b"\0")[0].decode(), dbf[o + 16])
            for o in range(32, header - 1, 32)
        ]
        records = []
        for i in range(count):
            row = dbf[header + i * size : header + (i + 1) * size]
            values = {}
            cursor = 1
            for name, length in fields:
                values[name] = row[cursor : cursor + length].decode("utf-8").strip()
                cursor += length
            records.append(values)
        offset = 100
        shapes = []
        while offset < len(shp):
            length = struct.unpack_from(">i", shp, offset + 4)[0] * 2
            body = shp[offset + 8 : offset + 8 + length]
            offset += 8 + length
            if struct.unpack_from("<i", body)[0] != 5:
                raise ValueError("expected_polygon")
            parts, points = struct.unpack_from("<ii", body, 36)
            starts = list(struct.unpack_from(f"<{parts}i", body, 44)) + [points]
            xy = [struct.unpack_from("<dd", body, 44 + parts * 4 + i * 16) for i in range(points)]
            if any(not (126 <= x <= 128 and 37 <= y <= 38) for x, y in xy):
                raise ValueError("invalid_coordinates")
            rings = [xy[starts[i] : starts[i + 1]] for i in range(parts)]
            if any(len(ring) < 4 or ring[0] != ring[-1] for ring in rings):
                raise ValueError("invalid_ring")
            shapes.append(rings)
        if len(records) != 121 or len(shapes) != 121 or len({r["AREA_CD"] for r in records}) != 121:
            raise ValueError("incomplete_official_places")
        return list(zip(records, shapes, strict=True))


async def main():
    engine = create_engine(Settings())
    try:
        async with async_sessionmaker(engine).begin() as session:
            features = []
            for values, rings in read_shapes(
                Path(__file__).parent / "data/citydata_places_official.zip"
            ):
                wkt = (
                    "MULTILINESTRING("
                    + ",".join("(" + ",".join(f"{x} {y}" for x, y in ring) + ")" for ring in rings)
                    + ")"
                )
                row = (
                    await session.execute(
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
                features.append(
                    {
                        "type": "Feature",
                        "id": values["AREA_CD"],
                        "geometry": json.loads(row[1]),
                        "properties": {
                            "area_code": values["AREA_CD"],
                            "name": values["AREA_NM"],
                            "category": values["CATEGORY"],
                            "topology_repaired": bool(row[2]),
                        },
                    }
                )
            await save_snapshot(
                session,
                "citydata_place_boundaries",
                {
                    "type": "FeatureCollection",
                    "features": features,
                    "source_url": "https://data.seoul.go.kr/dataVisual/seoul/guide.do",
                    "crs": "EPSG:4326",
                    "notice": (
                        "서울시 공식 121장소 관측 경계. 행정동 경계와 다릅니다. "
                        "파일의 기준일은 별도 확인 필요."
                    ),
                },
            )
            print("Official place polygons imported:", len(features))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
