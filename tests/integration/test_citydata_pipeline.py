import asyncio
import os
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.dependencies import get_session
from app.core.config import Settings
from app.db.session import create_engine
from app.main import create_app
from app.models.citydata import CityDataSnapshot
from app.models.external_api import ExternalApiObservation, ExternalApiResponse
from app.models.parking import CollectionRun
from app.modules.data_pipeline.pipelines import citydata


@pytest.mark.integration
def test_latest_history_update_and_archive_failure_rollback(monkeypatch):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL required")

    async def run():
        engine = create_engine(Settings(_env_file=None, database_url=url))
        factory = async_sessionmaker(engine, expire_on_commit=False)
        code = "TEST" + uuid4().hex
        data = {"AREA_CD": code, "AREA_NM": code, "LIVE_PPLTN_STTS": [{"count": 1}]}

        async def fetch():
            return data

        async def fail_archive(*args):
            raise RuntimeError("history unavailable")

        try:
            assert (await citydata.run_citydata(engine, fetch, code))["status"] == "success"
            data["LIVE_PPLTN_STTS"] = [{"count": 2}]
            assert (await citydata.run_citydata(engine, fetch, code))["status"] == "success"
            async with factory() as session:
                latest = await session.get(CityDataSnapshot, code)
                assert latest.data["LIVE_PPLTN_STTS"] == [{"count": 2}]
            app = create_app(Settings(_env_file=None, database_url=url))

            async def api_session():
                async with factory() as session:
                    yield session

            app.dependency_overrides[get_session] = api_session
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.get("/api/v1/citydata/areas/" + code)
                assert response.status_code == 200
                assert response.json()["data"]["LIVE_PPLTN_STTS"] == [{"count": 2}]
                async with factory() as session:
                    count = await session.scalar(
                        select(func.count())
                        .select_from(CityDataSnapshot)
                        .where(CityDataSnapshot.area_code == code)
                    )
                assert count == 1
                responses = (
                    select(ExternalApiResponse.id)
                    .join(CollectionRun, CollectionRun.id == ExternalApiResponse.run_id)
                    .where(CollectionRun.area_name == code)
                )
                async with factory() as session:
                    assert len(list(await session.scalars(responses))) == 2
            monkeypatch.setattr(citydata, "archive_response", fail_archive)
            data["LIVE_PPLTN_STTS"] = [{"count": 3}]
            result = await citydata.run_citydata(engine, fetch, code)
            assert result["status"] == "failed"
            async with factory() as session:
                latest = await session.get(CityDataSnapshot, code)
                assert latest.data["LIVE_PPLTN_STTS"] == [{"count": 2}]
        finally:
            async with factory.begin() as session:
                runs = select(CollectionRun.id).where(CollectionRun.area_name == code)
                responses = select(ExternalApiResponse.id).where(
                    ExternalApiResponse.run_id.in_(runs)
                )
                await session.execute(
                    delete(ExternalApiObservation).where(
                        ExternalApiObservation.response_id.in_(responses)
                    )
                )
                await session.execute(
                    delete(ExternalApiResponse).where(ExternalApiResponse.run_id.in_(runs))
                )
                await session.execute(delete(CollectionRun).where(CollectionRun.area_name == code))
                await session.execute(
                    delete(CityDataSnapshot).where(CityDataSnapshot.area_code == code)
                )
            await engine.dispose()

    asyncio.run(run())
