"""Run one official Seoul parking-data collection; AWS schedules this command externally."""

import asyncio
import json

import httpx

from app.clients.seoul import SeoulClient
from app.core.config import Settings
from app.db.session import create_engine
from app.modules.data_pipeline.collectors.parking import collect_parking
from app.modules.data_pipeline.normalizers.parking import SOURCE
from app.modules.data_pipeline.pipelines.parking import run_parking


async def execute() -> int:
    settings = Settings()
    if not settings.seoul_api_key.get_secret_value():
        print(json.dumps({"status": "failed", "error_code": "missing_api_key"}))
        return 1
    engine = create_engine(settings)
    try:
        async with httpx.AsyncClient() as http:
            client = SeoulClient(http, settings.seoul_api_key.get_secret_value())
            result = await run_parking(engine, lambda: collect_parking(client), SOURCE)
            print(json.dumps(result, ensure_ascii=False))
            return {"success": 0, "partial": 2, "skipped": 3}.get(result["status"], 1)
    finally:
        await engine.dispose()


def main() -> None:
    try:
        code = asyncio.run(execute())
    except KeyboardInterrupt:
        code = 130
    except Exception:
        print(json.dumps({"status": "failed", "error_code": "configuration_or_database_error"}))
        code = 1
    raise SystemExit(code)


if __name__ == "__main__":
    main()
