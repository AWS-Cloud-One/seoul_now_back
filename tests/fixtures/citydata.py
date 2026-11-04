"""Small current-response fixtures and typed place setup for direct-storage tests."""

from datetime import UTC, datetime, timedelta

from app.modules.data_pipeline.collectors.citydata import CityDataDocument
from app.modules.data_pipeline.identity import PROCESSOR_VERSION
from app.modules.data_pipeline.pipelines.citydata import run_citydata

STAMP = datetime(2026, 11, 4, 1, tzinfo=UTC)


def document(code="POI001", name="test"):
    return {
        "RESULT": {"RESULT.CODE": "INFO-000"},
        "CITYDATA": {
            "AREA_CD": code,
            "AREA_NM": name,
            "LIVE_PPLTN_STTS": [
                {
                    "AREA_CD": code,
                    "AREA_NM": name,
                    "AREA_PPLTN_MIN": "0",
                    "AREA_PPLTN_MAX": "10",
                    "AREA_CONGEST_LVL": "여유",
                    "REPLACE_YN": "N",
                    "PPLTN_TIME": "2026-11-04 09:00",
                }
            ],
            "PRK_STTS": [
                {
                    "PRK_CD": "1",
                    "PRK_NM": "parking",
                    "LAT": "37.5",
                    "LNG": "127",
                    "CPCTY": "10",
                    "CUR_PRK_YN": "Y",
                    "CUR_PRK_CNT": "0",
                    "CUR_PRK_TIME": "2026-11-04 09:00",
                }
            ],
            "LIVE_YNA_NEWS": [{"article": "unused content"}],
        },
    }


async def collect(engine, body, minute=0, **kwargs):
    async def fetch():
        return CityDataDocument(body, STAMP + timedelta(minutes=minute))

    return await run_citydata(engine, fetch, "test", expected_code="POI001", **kwargs)


async def boundary(factory, targets=None):
    # Set up exactly the canonical fields written by the explicit reference import.
    from sqlalchemy import text

    async with factory.begin() as session:
        for code, name in (targets or {"POI001": "test"}).items():
            await session.execute(
                text("""
                INSERT INTO data_sources(source_code,display_name,provider_name)
                VALUES ('seoul_citydata','test','test') ON CONFLICT DO NOTHING
            """)
            )
            await session.execute(
                text("""
                INSERT INTO population_areas(area_code,name,geometry,source,source_version,
                    processor_version,processed_at)
                VALUES (:code,:name,
                    ST_GeomFromText('MULTIPOLYGON(((126 37,128 37,128 38,126 38,126 37)))',4326),
                    'seoul_citydata','test',:processor,now())
                ON CONFLICT DO NOTHING
            """),
                {"code": code, "name": name, "processor": PROCESSOR_VERSION},
            )
