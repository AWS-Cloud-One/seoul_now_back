"""Project the latest full-area snapshots into spatial parking records."""

import asyncio
import json
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import Settings
from app.db.session import create_engine
from app.modules.data_pipeline.normalizers.parking import CITYDATA_PARKING_SOURCE, normalize_parking
from app.modules.data_pipeline.pipelines.parking import run_parking
from app.repositories.citydata import CityDataRepository


async def project(engine):
    async def fetch():
        async with async_sessionmaker(engine)() as session:
            snapshots = await CityDataRepository(session).list_latest()
        rows = {}
        rejected = []
        for snapshot in snapshots:
            for row in snapshot.data.get("PRK_STTS") or []:
                if not isinstance(row, dict):
                    rejected.append(row)
                    continue
                try:
                    normalized = normalize_parking(
                        row, snapshot.collected_at, CITYDATA_PARKING_SOURCE
                    )
                except ValueError:
                    rejected.append(row)
                    continue
                timestamp = normalized["source_updated_at"] or datetime.min.replace(tzinfo=UTC)
                previous = rows.get(normalized["id"])
                if previous is None or timestamp > previous[0]:
                    rows[normalized["id"]] = (timestamp, row)
        return [row for _, row in rows.values()] + rejected

    return await run_parking(engine, fetch, CITYDATA_PARKING_SOURCE)


async def main():
    engine = create_engine(Settings())
    try:
        print(json.dumps(await project(engine), ensure_ascii=False))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
