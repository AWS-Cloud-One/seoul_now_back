"""Collect Seoul municipal parking occupancy; no keys are printed."""

import asyncio
import json

import httpx
from geoalchemy2 import Geometry
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.clients.seoul import SeoulClient
from app.core.config import Settings
from app.db.session import create_engine
from app.models.parking import ParkingRecord
from app.modules.data_pipeline.collectors.parking import collect_parking
from app.modules.data_pipeline.normalizers.parking import MUNICIPAL_PARKING_SOURCE
from app.modules.data_pipeline.pipelines.parking import run_parking
from app.repositories.provider import save_snapshot


async def execute(interval=0):
    settings = Settings()
    engine = create_engine(settings)
    try:
        async with httpx.AsyncClient() as http:
            client = SeoulClient(
                http,
                (
                    settings.seoul_municipal_parking_api_key.get_secret_value()
                    or settings.seoul_api_key.get_secret_value()
                ),
                "GetParkingInfo",
            )

            async def fetch():
                rows = await collect_parking(client)
                async with async_sessionmaker(engine)() as session:
                    records = await session.execute(
                        select(
                            ParkingRecord.source_id,
                            func.ST_Y(ParkingRecord.location.cast(Geometry)),
                            func.ST_X(ParkingRecord.location.cast(Geometry)),
                        ).where(ParkingRecord.source == "seoul_getparkinfo")
                    )
                    positions = {}
                    for code, lat, lon in records:
                        positions.setdefault(code, set()).add((lat, lon))
                async with async_sessionmaker(engine).begin() as session:
                    await save_snapshot(session, "seoul_municipal_parking", {"rows": rows})
                for row in rows:
                    candidates = positions.get(str(row.get("PKLT_CD")), set())
                    if len(candidates) == 1:
                        row["LAT"], row["LOT"] = next(iter(candidates))
                return rows

            while True:
                result = await run_parking(engine, fetch, MUNICIPAL_PARKING_SOURCE)
                print(json.dumps(result, ensure_ascii=False), flush=True)
                if not interval:
                    return result
                await asyncio.sleep(interval)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval", type=int, default=0)
    args = parser.parse_args()
    if args.interval and args.interval < 60:
        parser.error("interval must be at least 60 seconds")
    result = asyncio.run(execute(args.interval))
    raise SystemExit({"success": 0, "partial": 2, "skipped": 3}.get(result["status"], 1))
