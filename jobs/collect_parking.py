"""Run one official Seoul parking-data collection; AWS schedules this command externally."""

import argparse

import httpx

from app.clients.seoul import SeoulClient
from app.core.config import Settings
from app.db.session import create_engine
from app.modules.data_pipeline.collectors.parking import collect_parking
from app.modules.data_pipeline.normalizers.parking import SOURCE
from app.modules.data_pipeline.pipelines.parking import run_parking
from jobs.runtime import finish, run_cli


async def execute() -> int:
    settings = Settings()
    if not settings.seoul_api_key.get_secret_value():
        return finish({"status": "failed", "error_code": "missing_api_key"})
    engine = create_engine(settings)
    try:
        async with httpx.AsyncClient() as http:
            client = SeoulClient(http, settings.seoul_api_key.get_secret_value())
            result = await run_parking(engine, lambda: collect_parking(client), SOURCE)
            return finish(result)
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    run_cli(execute)


if __name__ == "__main__":
    main()
