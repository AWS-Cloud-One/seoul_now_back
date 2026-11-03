"""Collect Seoul municipal parking occupancy; no keys are printed."""

import argparse
import asyncio

import httpx

from app.clients.seoul import SeoulClient
from app.core.config import Settings
from app.db.session import create_engine
from app.modules.data_pipeline.collectors.parking import collect_parking
from app.modules.data_pipeline.pipelines.parking import run_municipal_parking
from jobs.runtime import finish, run_cli


async def execute(interval=0) -> int:
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

            while True:
                result = await run_municipal_parking(engine, lambda: collect_parking(client))
                code = finish(result)
                if not interval:
                    return code
                await asyncio.sleep(interval)
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval", type=int, default=0)
    args = parser.parse_args()
    if args.interval and args.interval < 60:
        parser.error("interval must be at least 60 seconds")
    run_cli(execute, args.interval)


if __name__ == "__main__":
    main()
