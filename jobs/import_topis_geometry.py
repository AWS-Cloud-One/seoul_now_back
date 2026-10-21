"""Import official OA-15060 vertices; validate IDs, sequence and coordinate conversion."""

import asyncio
import hashlib
import json
import math
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import Settings
from app.db.session import create_engine
from app.repositories.citydata import CityDataRepository
from app.repositories.provider import save_snapshot

SOURCE = "topis_geometry:2025"
PATH = Path(__file__).parent / "data/topis_vertices_2025.xlsx"


def read_vertices(path=PATH):
    ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    seen = {}
    with zipfile.ZipFile(path) as book:
        strings = [
            "".join(si.itertext()) for si in ET.fromstring(book.read("xl/sharedStrings.xml"))
        ]
        sheet = ET.fromstring(book.read("xl/worksheets/sheet1.xml"))
        for row in sheet.findall(".//s:row", ns)[1:]:
            cells = {}
            for cell in row.findall("s:c", ns):
                node = cell.find("s:v", ns)
                if node is not None:
                    cells[re.sub(r"\d+", "", cell.get("r"))] = (
                        strings[int(node.text)] if cell.get("t") == "s" else node.text
                    )
            if not cells:
                continue
            link = cells.get("A", "")
            if not re.fullmatch(r"\d{10}", link):
                raise ValueError("invalid_official_link")
            seq_value = float(cells["B"])
            x, y = float(cells["C"]), float(cells["D"])
            if (
                not all(math.isfinite(n) for n in (seq_value, x, y))
                or not seq_value.is_integer()
                or seq_value < 1
            ):
                raise ValueError("invalid_vertex")
            key = (link, int(seq_value))
            if key in seen and seen[key] != (x, y):
                raise ValueError("conflicting_vertex_sequence")
            seen[key] = (x, y)
    groups = {}
    for (link, seq), _xy in seen.items():
        groups.setdefault(link, []).append(seq)
    if not groups or any(len(seq) < 2 for seq in groups.values()):
        raise ValueError("incomplete_vertex_sequence")
    return [
        {"link_id": link, "seq": seq, "x": xy[0], "y": xy[1]}
        for (link, seq), xy in sorted(seen.items())
    ]


def read_standard_mapping():
    path = Path(__file__).parent / "data/topis_standard_mapping_20250306.xlsx"
    ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    services = {}
    with zipfile.ZipFile(path) as book:
        strings = [
            "".join(si.itertext()) for si in ET.fromstring(book.read("xl/sharedStrings.xml"))
        ]
        sheet = ET.fromstring(book.read("xl/worksheets/sheet1.xml"))
        for row in sheet.findall(".//s:row", ns)[1:]:
            values = {}
            for cell in row.findall("s:c", ns):
                node = cell.find("s:v", ns)
                if node is not None:
                    values[re.sub(r"\d+", "", cell.get("r"))] = (
                        strings[int(node.text)] if cell.get("t") == "s" else node.text
                    )
            if not values:
                continue
            service, standard = values["A"], values["C"]
            seq = int(values.get("B") or 0)
            if (
                not re.fullmatch(r"\d{10}", service)
                or not re.fullmatch(r"\d{10}", standard)
                or seq < 0
            ):
                raise ValueError("invalid_official_mapping")
            services.setdefault(service, []).append((seq, standard))
    return {k: [standard for _, standard in sorted(set(v))] for k, v in services.items()}


async def execute():
    points = read_vertices()
    sequences = {}
    for point in points:
        sequences.setdefault(point["link_id"], []).append(point["seq"])
    excluded = sorted(
        link for link, seq in sequences.items() if sorted(seq) != list(range(1, len(seq) + 1))
    )
    points = [p for p in points if p["link_id"] not in excluded]
    mapping = read_standard_mapping()
    engine = create_engine(Settings())
    try:
        async with async_sessionmaker(engine).begin() as session:
            result = await session.execute(
                text("""
                SELECT link_id, ST_AsGeoJSON(ST_Transform(
                    ST_MakeLine(ST_SetSRID(ST_MakePoint(x,y),5181) ORDER BY seq),4326))
                FROM jsonb_to_recordset(CAST(:points AS jsonb)) AS p(
                    link_id text,seq int,x float8,y float8)
                GROUP BY link_id ORDER BY link_id
            """),
                {"points": json.dumps(points)},
            )
            features = []
            for link, geometry_json in result:
                geometry = json.loads(geometry_json)
                if not all(126 <= x <= 128 and 37 <= y <= 38 for x, y in geometry["coordinates"]):
                    raise ValueError("transformed_geometry_out_of_region")
                features.append(
                    {
                        "type": "Feature",
                        "id": link,
                        "geometry": geometry,
                        "properties": {
                            "service_link_id": link,
                            "standard_link_ids": mapping.get(link, []),
                            "geometry_source": "OA-15060",
                            "source_year": 2025,
                            "source_crs": "EPSG:5181",
                        },
                    }
                )
            city_links = {}
            for snapshot in await CityDataRepository(session).list_latest():
                traffic = snapshot.data.get("ROAD_TRAFFIC_STTS") or {}
                if isinstance(traffic, dict):
                    for row in traffic.get("ROAD_TRAFFIC_STTS") or []:
                        if isinstance(row, dict) and row.get("LINK_ID"):
                            city_links[str(row["LINK_ID"])] = row
            inverse = {}
            for service, standards in mapping.items():
                for standard in standards:
                    inverse.setdefault(standard, set()).add(service)
            official = {f["id"]: f for f in features}
            pairs = [
                (standard, service)
                for standard in sorted(city_links)
                for service in sorted(inverse.get(standard, set()))
                if service in official
            ]
            distances = []
            for standard, service in pairs[:: max(1, len(pairs) // 100)][:100]:
                try:
                    xy = [
                        [float(v) for v in p.split("_")]
                        for p in city_links[standard]["XYLIST"].split("|")
                    ]
                    if len(xy) < 2:
                        continue
                    distance = await session.scalar(
                        text("""
                        SELECT ST_Distance(
                            ST_Transform(ST_SetSRID(ST_GeomFromGeoJSON(:a),4326),5181),
                            ST_Transform(ST_SetSRID(ST_GeomFromGeoJSON(:b),4326),5181))
                    """),
                        {
                            "a": json.dumps(official[service]["geometry"]),
                            "b": json.dumps({"type": "LineString", "coordinates": xy}),
                        },
                    )
                    distances.append(float(distance))
                except KeyError, ValueError, TypeError:
                    continue
            if len(distances) < 10 or sorted(distances)[len(distances) // 2] > 30:
                print(
                    {
                        "samples": len(distances),
                        "median": sorted(distances)[len(distances) // 2] if distances else None,
                    }
                )
                raise ValueError("coordinate_cross_validation_failed")
            matched = {standard for standard, _ in pairs}
            report = {
                "file_links": len(sequences),
                "excluded_sequence_gap_links": excluded,
                "geometry_links": len(features),
                "vertices": len(points),
                "citydata_standard_links": len(city_links),
                "mapped_citydata_links": len(matched),
                "mapped_service_links": len({service for _, service in pairs}),
                "citydata_unmapped": sorted(set(city_links) - matched),
                "one_to_many_standard_links": sum(len(inverse[k]) > 1 for k in matched),
                "mapping_source": "OA-15061 2025-03-06",
                "crs_basis": (
                    "GRS80TM interpreted as EPSG:5181; "
                    "checked against officially mapped citydata WGS84 lines"
                ),
                "crs_check_samples": len(distances),
                "median_line_distance_m": round(sorted(distances)[len(distances) // 2], 2),
                "source_sha256": hashlib.sha256(PATH.read_bytes()).hexdigest(),
            }
            await save_snapshot(
                session,
                SOURCE,
                {
                    "type": "FeatureCollection",
                    "features": features,
                    "report": report,
                    "standard_to_services": {k: sorted(v) for k, v in inverse.items()},
                },
            )
            print(
                json.dumps(
                    {k: v for k, v in report.items() if k != "citydata_unmapped"},
                    ensure_ascii=False,
                )
            )
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(execute())
