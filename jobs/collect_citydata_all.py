"""Collect every hotspot from Seoul's official OA-21285 workbook."""

import argparse
import asyncio
import json
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import httpx

from app.core.config import Settings
from app.db.session import create_engine
from app.modules.data_pipeline.collectors.citydata import collect_citydata
from app.modules.data_pipeline.pipelines.citydata import run_citydata
from jobs.project_citydata_parking import project


def areas():
    ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(Path(__file__).parent / "data/seoul_areas.xlsx") as book:
        strings = [
            "".join(si.itertext()) for si in ET.fromstring(book.read("xl/sharedStrings.xml"))
        ]
        sheet = ET.fromstring(book.read("xl/worksheets/sheet1.xml"))
        result = {}
        for row in sheet.findall(".//s:row", ns):
            values = []
            for cell in row.findall("s:c", ns):
                value = cell.find("s:v", ns)
                if value is not None:
                    values.append(strings[int(value.text)] if cell.get("t") == "s" else value.text)
            for index, value in enumerate(values):
                if re.fullmatch(r"POI\d{3}", value) and index + 1 < len(values):
                    result[value] = values[index + 1]
        if len(result) != 121:
            raise ValueError("official_area_list_invalid")
        return result


async def main_async(interval):
    settings = Settings()
    key = settings.seoul_citydata_api_key.get_secret_value()
    if not key:
        raise ValueError("missing_citydata_api_key")
    targets = areas()
    engine = create_engine(settings)
    try:
        async with httpx.AsyncClient() as client:
            while True:
                success = 0
                for code, name in targets.items():
                    result = await run_citydata(
                        engine, lambda name=name: collect_citydata(client, key, name), name
                    )
                    success += result["status"] == "success"
                    print(
                        json.dumps(
                            {
                                "area_code": code,
                                "area_name": name,
                                "status": result["status"],
                                "error_code": result.get("error_code"),
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                    await asyncio.sleep(0.25)
                print(
                    json.dumps(
                        {
                            "cycle_complete": True,
                            "total": len(targets),
                            "success": success,
                            "failed": len(targets) - success,
                        }
                    ),
                    flush=True,
                )
                print(json.dumps(await project(engine), ensure_ascii=False), flush=True)
                if not interval:
                    break
                await asyncio.sleep(interval)
    finally:
        await engine.dispose()


if __name__ == "__main__":
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
    asyncio.run(main_async(args.interval))
