"""Collect live parking status for one Seoul Real-Time City Data hotspot."""

import argparse
import asyncio
import json

import httpx

from app.core.config import Settings
from app.db.session import create_engine
from app.modules.data_pipeline.collectors.citydata_parking import collect_citydata_parking
from app.modules.data_pipeline.normalizers.parking import CITYDATA_PARKING_SOURCE
from app.modules.data_pipeline.pipelines.parking import run_parking


async def execute(area_name: str) -> int:
    settings = Settings()
    key = settings.seoul_citydata_api_key.get_secret_value()
    if not key:
        print(json.dumps({"status": "failed", "error_code": "missing_citydata_api_key"}))
        return 1
    engine = create_engine(settings)
    try:
        async with httpx.AsyncClient() as http:
            result = await run_parking(
                engine,
                lambda: collect_citydata_parking(http, key, area_name),
                CITYDATA_PARKING_SOURCE,
            )
            result["area_name"] = area_name
            print(json.dumps(result, ensure_ascii=False))
            return {"success": 0, "partial": 2, "skipped": 3}.get(result["status"], 1)
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--area", required=True, help="서울시 주요 121장소 중 하나의 장소명")
    args = parser.parse_args()
    try:
        code = asyncio.run(execute(args.area))
    except KeyboardInterrupt:
        code = 130
    except Exception:
        print(json.dumps({"status": "failed", "error_code": "configuration_or_database_error"}))
        code = 1
    raise SystemExit(code)


if __name__ == "__main__":
    main()
