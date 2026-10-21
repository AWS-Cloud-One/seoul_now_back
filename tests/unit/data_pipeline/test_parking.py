import asyncio
from datetime import UTC, datetime

import httpx
import pytest

from app.clients.seoul import CollectionError, SeoulClient
from app.modules.data_pipeline.collectors.parking import collect_parking
from app.modules.data_pipeline.normalizers.parking import normalize_parking


def row(**changes):
    return {
        "PKLT_CD": "001",
        "PKLT_NM": "test",
        "LAT": "37.56",
        "LOT": "126.97",
        "PRK_CRG": "",
        **changes,
    }


@pytest.mark.parametrize("lat,lon", [(0, 0), (None, 127), ("nan", 127), (37, "inf")])
def test_reject_invalid_coordinates(lat, lon):
    with pytest.raises(ValueError, match="coordinates"):
        normalize_parking(row(LAT=lat, LOT=lon), datetime.now(UTC))


def test_preserves_sections_and_unknowns():
    now = datetime.now(UTC)
    a = normalize_parking(row(), now)
    b = normalize_parking(row(LOT="126.971"), now)
    assert a["id"] != b["id"]  # Same code is not necessarily the same section.
    assert a["details"]["PRK_CRG"] == ""
    assert a["source_updated_at"] is None
    assert "availability" not in a


def test_complete_pagination():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        start = int(request.url.path.split("/")[-3])
        rows = [row(PKLT_CD=str(start))]
        return httpx.Response(
            200,
            json={
                "GetParkInfo": {
                    "RESULT": {"CODE": "INFO-000"},
                    "list_total_count": 2,
                    "row": rows,
                }
            },
        )

    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            assert len(await collect_parking(SeoulClient(http, "test"), page_size=1)) == 2

    asyncio.run(check())
    assert len(calls) == 2


def test_auth_errors_are_not_retried_or_leaked():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(
            200, json={"RESULT": {"CODE": "INFO-100", "MESSAGE": "secret-do-not-log"}}
        )

    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            with pytest.raises(CollectionError, match="^provider_result_error$"):
                await collect_parking(SeoulClient(http, "secret-key"))

    asyncio.run(check())
    assert len(calls) == 1


def test_truncated_snapshot_fails():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "GetParkInfo": {
                    "RESULT": {"CODE": "INFO-000"},
                    "list_total_count": 2,
                    "row": [row()],
                }
            },
        )

    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            with pytest.raises(CollectionError, match="snapshot_incomplete"):
                await collect_parking(SeoulClient(http, "test"))

    asyncio.run(check())
