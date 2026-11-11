"""Part 1 queries only: provider/DB facts are mocked, never run collectors."""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.clients.area_extras import SeoulAreaExtras
from app.clients.kakao import KakaoError, KakaoLocalClient
from app.clients.query_kakao import KakaoQueryClient
from app.core.exceptions import ServiceError
from app.schemas.location import Coordinates
from app.schemas.query_services import ParkingSearchQuery
from app.services.area_queries import WEATHER_FIELDS, AreaQueryService, section_information
from app.services.locations import LocationSearchService
from app.services.parking_nearby import ParkingNearbyService
from app.services.parking_search import ParkingSearchService, comparable_fee
from app.services.place_queries import PlaceQueryService
from app.services.query_cursors import QueryCursorStore


def point():
    return Coordinates(latitude=37.5665, longitude=126.978)


def test_cursor_condition_binding_expiry_capacity_and_replay():
    async def run():
        clock = [0]
        store = QueryCursorStore(ttl_seconds=5, capacity=1, clock=lambda: clock[0])
        token = await store.issue(("query", 10), {"page": 2})
        assert await store.read(token, ("query", 10)) == {"page": 2}
        changed = await store.read(token, ("query", 10))
        changed["page"] = 3
        assert (await store.read(token, ("query", 10)))["page"] == 2
        with pytest.raises(ServiceError) as error:
            await store.read(token, ("query", 11))
        assert error.value.code == "INVALID_CURSOR"
        with pytest.raises(ServiceError):
            await store.issue("another", {})
        clock[0] = 5
        with pytest.raises(ServiceError) as error:
            await store.read(token, ("query", 10))
        assert error.value.code == "CURSOR_EXPIRED"
        assert await store.issue("another", {})

    asyncio.run(run())


def test_address_next_page_and_changed_query_rejected():
    async def run():
        provider = AsyncMock()
        provider.keyword.return_value = {"documents": [], "meta": {"is_end": True}}

        def row(label):
            return {"address_name": label, "x": "126.978", "y": "37.5665"}

        provider.address.side_effect = [
            {"documents": [row("one"), row("two")], "meta": {"is_end": False}},
            {"documents": [row("three")], "meta": {"is_end": True}},
        ]
        provider.region.return_value = {
            "documents": [{"region_type": "B", "region_1depth_name": "서울특별시"}]
        }
        service = LocationSearchService(provider)
        one = await service.search("주소", None, 1)
        two = await service.search("주소", one.next_cursor, 1)
        assert two.items[0].label == "two"
        assert provider.address.call_args.kwargs["page"] == 2
        with pytest.raises(ServiceError) as error:
            await service.search("다른주소", one.next_cursor, 1)
        assert error.value.code == "INVALID_CURSOR"

    asyncio.run(run())


def test_nearby_cursor_version_change_and_same_radius():
    async def run():
        provider, repo = AsyncMock(), AsyncMock()
        provider.region.return_value = {
            "documents": [{"region_type": "B", "region_1depth_name": "서울특별시"}]
        }
        repo.get_version.return_value = SimpleNamespace(is_ready=True, geometry_version="v1")
        repo.summarize_radius.side_effect = [(0, None), (2, 450), (2, 450)]
        repo.list_facilities.return_value = [
            (uuid4(), "unverified", {"name": "주차"}, 37.5665, 126.978, 450)
        ]
        repo.list_records.return_value = {}
        service = ParkingNearbyService(repo, provider, cursors=QueryCursorStore())
        first = await service.search(point(), 1, None)
        second = await service.search(point(), 1, first.parking.next_cursor)
        assert second.parking.search_radius_meters == 500
        assert [c.args[1] for c in repo.summarize_radius.call_args_list] == [300, 500, 500]
        repo.get_version.return_value.geometry_version = "v2"
        with pytest.raises(ServiceError) as error:
            await service.search(point(), 1, first.parking.next_cursor)
        assert error.value.code == "CURSOR_EXPIRED"

    asyncio.run(run())


@pytest.mark.parametrize(
    "records,expected",
    [
        ([(False, None, None)], (0.0, "free")),
        ([(True, 10, 500)], (3000.0, "paid")),
        ([(True, None, 500)], (None, "unknown")),
        ([(True, 10, 0)], (None, "unknown")),
        ([(False, None, None), (True, 10, 500)], (None, "conflict")),
        ([(True, 10, 500), (True, 10, 1000)], (None, "conflict")),
        ([(False, None, None), (None, None, None)], (0.0, "free")),
        ([(True, 10, 500), (None, None, None)], (3000.0, "paid")),
    ],
)
def test_fee_comparison_does_not_guess_free_or_combine_sources(records, expected):
    values = [
        SimpleNamespace(
            is_paid=paid,
            base_minutes=Decimal(minutes) if minutes else None,
            base_fee_krw=Decimal(amount) if amount is not None else None,
            base_usable=True,
        )
        for paid, minutes, amount in records
    ]
    assert comparable_fee(values)[:2] == expected


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize(
    "minutes,amount,usable",
    [(None, 500, False), (10, None, False), (10, 500, False), (None, 500, True)],
    ids=["missing-time", "missing-fee", "unusable-base", "missing-time-marked-usable"],
)
def test_free_and_paid_conflict_even_when_paid_fee_is_unusable(minutes, amount, usable, reverse):
    records = [
        SimpleNamespace(is_paid=False),
        SimpleNamespace(
            is_paid=True,
            base_minutes=Decimal(minutes) if minutes is not None else None,
            base_fee_krw=Decimal(amount) if amount is not None else None,
            base_usable=usable,
        ),
    ]
    if reverse:
        records.reverse()
    assert comparable_fee(records) == (None, "conflict", "CONFLICTING_SOURCE_FEES")


def test_parking_search_fixed_radius_pages_and_changed_order_expiry():
    async def run():
        repo = AsyncMock()
        repo.get_version.return_value = SimpleNamespace(is_ready=True, geometry_version="v1")
        keys = [uuid4(), uuid4()]
        repo.list_facilities.return_value = [
            (key, "unverified", {"name": "주차"}, 37.5665, 126.978, meters)
            for key, meters in zip(keys, [100, 50], strict=True)
        ]
        repo.list_records.return_value = {}
        service = ParkingSearchService(repo, cursors=QueryCursorStore())
        query = ParkingSearchQuery(latitude=37.5665, longitude=126.978, limit=1, radius_meters=2000)
        first = await service.search(query)
        assert first.items[0].facility.parking_id == keys[1]
        second = await service.search(query.model_copy(update={"cursor": first.next_cursor}))
        assert second.items[0].facility.parking_id == keys[0] and second.next_cursor is None
        assert repo.list_facilities.call_args.args[1] == 2000
        with pytest.raises(ServiceError):
            await service.search(
                query.model_copy(update={"cursor": first.next_cursor, "sort": "price"})
            )
        repo.get_version.return_value.geometry_version = "v2"
        with pytest.raises(ServiceError) as error:
            await service.search(query.model_copy(update={"cursor": first.next_cursor}))
        assert error.value.code == "CURSOR_EXPIRED"

    asyncio.run(run())


def route_body(*, summary=None, roads=None):
    return {
        "routes": [
            {
                "result_code": 0,
                "summary": {"duration": 600, "distance": 1000} if summary is None else summary,
                "sections": [
                    {
                        "roads": [{"vertexes": [126.978, 37.5665, 126.98, 37.57]}]
                        if roads is None
                        else roads
                    }
                ],
            }
        ]
    }


def test_driving_preserves_route_without_fares_and_separate_geometry():
    async def run():
        provider = AsyncMock()
        provider.driving.return_value = route_body()
        service = PlaceQueryService(provider)
        result = await service.driving(point(), point())
        assert result.route.value.duration_seconds == 600
        assert result.route.value.taxi_fare_krw is None
        assert result.route.value.geometry.coordinates[0][0] == (126.978, 37.5665)
        assert result.route.observed_at is None and result.route.collected_at
        provider.driving.return_value = route_body(roads=[{"vertexes": [1]}])
        result = await service.driving(point(), point())
        assert result.route.value.geometry is None and result.route.value.duration_seconds == 600

    asyncio.run(run())


@pytest.mark.parametrize(
    "body,code",
    [
        ({"routes": [{"result_code": 1}]}, "NOT_FOUND"),
        (route_body(summary={"duration": True, "distance": 1}), "PROVIDER_ERROR"),
        (route_body(summary={"duration": -1, "distance": 1}), "PROVIDER_ERROR"),
        ({"routes": []}, "PROVIDER_ERROR"),
        ({"routes": [{"result_code": 999}]}, "PROVIDER_ERROR"),
    ],
)
def test_route_none_and_invalid_numbers_are_not_zero_eta(body, code):
    async def run():
        provider = AsyncMock()
        provider.driving.return_value = body
        with pytest.raises(ServiceError) as error:
            await PlaceQueryService(provider).driving(point(), point())
        assert error.value.code == code

    asyncio.run(run())


def test_places_category_radius_paging_and_optional_fields():
    async def run():
        provider = AsyncMock()

        def row(identifier, *, x=126.978, category="CE7"):
            return {
                "id": identifier,
                "place_name": identifier,
                "x": x,
                "y": 37.5665,
                "category_group_code": category,
            }

        provider.places.side_effect = [
            {
                "documents": [
                    row("a"),
                    row("b"),
                    row("outside", x=128),
                    row("food", category="FD6"),
                ],
                "meta": {"is_end": True},
            },
        ]
        service = PlaceQueryService(provider, cursors=QueryCursorStore())
        one = await service.search("카페", 1, center=point(), radius=800, category="CE7")
        two = await service.search(
            "카페", 1, one.next_cursor, center=point(), radius=800, category="CE7"
        )
        assert [one.items[0].place_id, two.items[0].place_id] == ["kakao:a", "kakao:b"]
        assert two.next_cursor is None and two.items[0].phone is None
        provider.places.assert_awaited_once()

    asyncio.run(run())


def test_shared_query_transport_does_not_change_existing_directions():
    async def run():
        calls = []

        def respond(request):
            calls.append(request)
            return httpx.Response(200, json=route_body())

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            client = KakaoLocalClient(http, "test")
            queries = KakaoQueryClient(client)
            await queries.driving(point(), point(), include_geometry=True)
            await client.directions(point(), point())
            assert calls[0].url.params["summary"] == "false"
            assert calls[1].url.params["summary"] == "true"
            assert calls[0].url.params["origin"] == "126.978,37.5665"

    asyncio.run(run())


def test_area_partial_failure_keeps_other_facts_and_extras_are_opt_in():
    async def run():
        repo, extras = AsyncMock(), AsyncMock()
        repo.area.return_value = SimpleNamespace(name="관측영역")

        async def read(area, layer):
            if layer == "roads":
                raise SQLAlchemyError("private-db-details")
            return (True, None if layer == "population" else 0)

        repo.read_section.side_effect = read
        result = await AreaQueryService(repo, extras).overview("POI001")
        assert result["roads"]["status"] == "error"
        assert result["parking"]["value"]["registered_facility_count"] == 0
        assert result["population"]["value"] is None
        extras.fetch.assert_not_awaited()
        extras.fetch.side_effect = ServiceError("PROVIDER_TIMEOUT", "시간 초과")
        result = await AreaQueryService(repo, extras).overview("POI001", include_extras=True)
        assert result["weather"]["status"] == "error"
        assert result["parking"]["status"] == "available"

    asyncio.run(run())


def test_weather_time_numeric_validation_and_no_receipt_substitution():
    now = datetime.now(UTC)
    old = (now - timedelta(hours=2)).isoformat()
    info = section_information(
        {"TEMP": "-3.5", "PM25": "NaN", "WEATHER_TIME": old},
        WEATHER_FIELDS,
        "WEATHER_TIME",
        now,
        1800,
    )
    assert info["value"]["temperature_celsius"] == -3.5 and info["value"]["pm25"] is None
    assert info["freshness"] == "stale" and info["observed_at"] != info["collected_at"]
    info = section_information({"TEMP": "0"}, WEATHER_FIELDS, "WEATHER_TIME", now, 1800)
    assert info["value"]["temperature_celsius"] == 0 and info["observed_at"] is None


def test_commerce_keeps_valid_categories_when_area_totals_are_missing():
    from app.services.area_queries import COMMERCE_FIELDS

    now = datetime.now(UTC)
    info = section_information(
        {"categories": [{"RSB_LRG_CTGR": "음식", "RSB_MCT_CNT": "12"}]},
        COMMERCE_FIELDS,
        "CMRCL_TIME",
        now,
        1800,
        commerce=True,
    )
    assert info["status"] == "available"
    assert info["value"]["level"] is None
    assert info["value"]["categories"][0]["merchant_count"] == 12
    assert info["observed_at"] is None


def test_area_extras_selects_fields_and_rejects_wrong_identity_without_writes():
    async def run():
        body = {
            "RESULT": {"CODE": "INFO-000"},
            "CITYDATA": {
                "AREA_CD": "POI001",
                "AREA_NM": "관측영역",
                "WEATHER_STTS": {"TEMP": "10"},
                "LIVE_CMRCL_STTS": {"AREA_CMRCL_LVL": "보통"},
                "private_other_section": "unused",
            },
        }
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body))
        ) as http:
            client = SeoulAreaExtras(http, "private-key")
            selected = await client.fetch("POI001", "관측영역")
            assert set(selected) == {"WEATHER_STTS", "LIVE_CMRCL_STTS"}
            with pytest.raises(ServiceError) as error:
                await client.fetch("POI002", "관측영역")
            assert error.value.code == "PROVIDER_ERROR"
            assert "private-key" not in str(error.value)

    asyncio.run(run())


@pytest.mark.parametrize(
    "status,code", [(429, "RATE_LIMITED"), (502, "PROVIDER_ERROR"), (504, "PROVIDER_TIMEOUT")]
)
def test_query_provider_failure_is_not_empty_success(status, code):
    async def run():
        provider = AsyncMock()
        provider.driving.side_effect = KakaoError(status, "안전한 오류")
        with pytest.raises(ServiceError) as error:
            await PlaceQueryService(provider).driving(point(), point())
        assert error.value.code == code

    asyncio.run(run())
