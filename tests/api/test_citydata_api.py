import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.routes import citydata


def test_citydata_area_endpoint_returns_unfiltered_complete_payload(monkeypatch):
    payload = {
        "AREA_NM": "광화문·덕수궁",
        "AREA_CD": "POI001",
        "LIVE_PPLTN_STTS": [{"AREA_PPLTN_MIN": "100"}],
        "NEW_FIELD": [1, 2],
    }
    snapshot = SimpleNamespace(
        area_code="POI001", area_name="광화문·덕수궁", data=payload, collected_at=datetime.now(UTC)
    )

    async def get(self, code):
        assert code == "POI001"
        return snapshot

    monkeypatch.setattr(citydata.CityDataRepository, "get", get)
    response = asyncio.run(citydata.get_area("POI001", object()))
    assert response["data"] == payload


def test_citydata_area_endpoint_returns_404_when_uncollected(monkeypatch):
    async def get(self, code):
        return None

    monkeypatch.setattr(citydata.CityDataRepository, "get", get)
    with pytest.raises(HTTPException) as error:
        asyncio.run(citydata.get_area("missing", object()))
    assert error.value.status_code == 404
