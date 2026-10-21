"""Bounded TOPIS collection selected through the official standard/service mapping."""

import argparse
import asyncio
import json

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import Settings
from app.db.session import create_engine
from app.models.provider import ProviderSnapshot
from app.repositories.citydata import CityDataRepository
from jobs.collect_topis import execute as collect_links


async def targets(area_code, max_links):
    engine = create_engine(Settings())
    try:
        async with async_sessionmaker(engine)() as session:
            geometry = await session.get(ProviderSnapshot, "topis_geometry:2025")
            if geometry is not None and area_code is None:
                links = sorted({f["id"] for f in geometry.data["features"]})
                return links, {
                    "scope": "all_official_geometry",
                    "selected_links": len(links),
                    "truncated": False,
                }
            area = await CityDataRepository(session).get(area_code)
            if geometry is None or area is None:
                raise ValueError("missing_geometry_or_area")
            road = area.data.get("ROAD_TRAFFIC_STTS") or {}
            rows = road.get("ROAD_TRAFFIC_STTS") or []
            inverse = geometry.data["standard_to_services"]
            valid = {f["id"] for f in geometry.data["features"]}
            standards = {
                str(row["LINK_ID"]) for row in rows if isinstance(row, dict) and row.get("LINK_ID")
            }
            links = sorted(
                {
                    link
                    for standard in standards
                    for link in inverse.get(standard, [])
                    if link in valid
                }
            )
            if not links:
                raise ValueError("no_official_mapping_for_area")
            return links[:max_links], {
                "area_code": area_code,
                "mapped_service_links": len(links),
                "selected_links": min(len(links), max_links),
                "truncated": len(links) > max_links,
                "unmapped_standard_links": len(
                    [s for s in standards if not any(link in valid for link in inverse.get(s, []))]
                ),
            }
    finally:
        await engine.dispose()


async def run(area, max_links, interval):
    while True:
        links, report = await targets(area, max_links)
        print(json.dumps(report), flush=True)
        result = await collect_links(links)
        if not interval:
            return result
        await asyncio.sleep(max(interval, 3600) if result == 4 else interval)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--area", help="Collected citydata AREA_CD")
    scope.add_argument("--all", action="store_true", help="Every validated official service link")
    parser.add_argument("--max-links", type=int, default=20)
    parser.add_argument(
        "--interval", type=int, default=0, help="Optional seconds between cycles; default is once"
    )
    args = parser.parse_args()
    if not 1 <= args.max_links <= 200 or args.interval and args.interval < 300:
        parser.error("max-links must be 1..200; interval must be 0 or at least 300")
    raise SystemExit(asyncio.run(run(args.area, args.max_links, args.interval)))
