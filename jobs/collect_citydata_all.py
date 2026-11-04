"""CLI for collecting every official hotspot and projecting its parking data."""

import argparse
import asyncio

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import Settings
from app.db.session import create_engine
from app.modules.data_pipeline.collectors.citydata import collect_citydata
from app.modules.data_pipeline.pipelines.citydata import run_citydata_batch
from app.repositories.reference import ReferenceRepository
from jobs.runtime import finish, report, run_cli


async def execute(interval=0) -> int:
    settings = Settings()
    key = settings.seoul_citydata_api_key.get_secret_value()
    if not key:
        return finish({"status": "failed", "error_code": "missing_citydata_api_key"})
    engine = create_engine(settings)
    try:
        async with httpx.AsyncClient() as client:
            while True:
                async with async_sessionmaker(engine)() as session:
                    targets = await ReferenceRepository(session).citydata_areas()
                if not targets:
                    return finish(
                        {"status": "failed", "error_code": "import_place_reference_first"}
                    )
                result = await run_citydata_batch(
                    engine,
                    lambda code, name: collect_citydata(client, key, name, expected_code=code),
                    targets,
                    report=report,
                )
                code = finish(result)
                if not interval:
                    return code
                await asyncio.sleep(max(interval, 3600) if code == 4 else interval)
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--interval",
        type=int,
        default=0,
        help="Seconds between full collection cycles; 0 runs once",
    )
    args = parser.parse_args()
    if args.interval and args.interval < 300:
        parser.error("interval must be at least 300 seconds")
    run_cli(execute, args.interval)


if __name__ == "__main__":
    main()
