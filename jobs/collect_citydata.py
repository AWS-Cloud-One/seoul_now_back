"""Collect one complete Seoul OA-21285 integrated city-data snapshot."""

import argparse

import httpx

from app.core.config import Settings
from app.db.session import create_engine
from app.modules.data_pipeline.collectors.citydata import (
    collect_citydata,
    read_citydata_areas,
    resolve_citydata_area,
)
from app.modules.data_pipeline.pipelines.citydata import run_citydata
from jobs.runtime import REFERENCE_DATA, finish, run_cli


async def execute(area_name: str) -> int:
    settings = Settings()
    key = settings.seoul_citydata_api_key.get_secret_value()
    if not key:
        return finish({"status": "failed", "error_code": "missing_citydata_api_key"})
    area_code, area_name = resolve_citydata_area(
        area_name, read_citydata_areas(REFERENCE_DATA / "seoul_areas.xlsx")
    )
    engine = create_engine(settings)
    try:
        async with httpx.AsyncClient() as http:
            result = await run_citydata(
                engine,
                lambda: collect_citydata(http, key, area_name, expected_code=area_code),
                area_name,
                expected_code=area_code,
            )
            return finish(result)
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--area", required=True, help="공식 서울 주요장소명 또는 장소 코드")
    args = parser.parse_args()
    run_cli(execute, args.area)


if __name__ == "__main__":
    main()
