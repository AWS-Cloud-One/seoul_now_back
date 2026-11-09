import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.clients.area_extras import SeoulAreaExtras
from app.core.exceptions import ServiceError
from app.modules.recommendation.areas import SearchArea
from app.schemas.location import Coordinates
from app.services.recommendation_geography import (
    SOURCE,
    RecommendationGeographyService,
    RoadGrade,
    consensus,
    parse_grades,
)
from tests.unit.recommendation.test_progress import next_request, request, setup

NOW = datetime.now(UTC)
CENTER = Coordinates(latitude=37.57, longitude=126.98)


def sections(*grades, at=None):
    return {
        "ROAD_TRAFFIC_STTS": {
            "AVG_ROAD_DATA": {
                "ROAD_TRAFFIC_TIME": at or NOW.isoformat(),
                "ROAD_TRAFFIC_IDX": "정체",
            },
            "ROAD_TRAFFIC_STTS": [
                {"LINK_ID": link, "IDX": grade, "SPD": 99} for link, grade in grades
            ],
        }
    }


def area():
    return SearchArea(
        area_id="dong",
        name="행정동",
        center=CENTER,
        source="sgis_areas",
        geometry_version="v1",
        polygons=[
            [
                [(126.97, 37.56), (126.99, 37.56), (126.99, 37.58), (126.97, 37.58)],
                [(126.971, 37.561), (126.973, 37.561), (126.973, 37.563), (126.971, 37.563)],
            ],
            [[(127.0, 37.59), (127.02, 37.59), (127.02, 37.61), (127.0, 37.61)]],
        ],
    )


def test_multipolygon_holes_and_boundary():
    a = area()
    assert a.contains(CENTER)
    assert a.contains(Coordinates(longitude=127.01, latitude=37.60))
    assert not a.contains(Coordinates(longitude=126.972, latitude=37.562))
    assert not a.contains(Coordinates(longitude=126.971, latitude=37.562))
    assert a.contains(Coordinates(longitude=126.97, latitude=37.57))
    assert not a.contains(Coordinates(longitude=127.03, latitude=37.60))


def test_original_link_grade_and_korean_time_not_speed_or_hotspot_average():
    data = parse_grades(sections(("standard", "서행"), at="2026-11-09 12:00"))
    value = data["standard"]
    assert value.grade == "서행" and value.source == SOURCE
    assert value.observed_at == datetime(2026, 11, 9, 3, tzinfo=UTC)
    assert parse_grades(sections(("standard", None)))["standard"].grade is None
    assert parse_grades({"ROAD_TRAFFIC_STTS": []}) == {}
    duplicate = parse_grades(sections(("a", "원활"), ("a", "정체"), ("a", "원활")))
    assert duplicate["a"].grade is None


@pytest.mark.parametrize(
    "mode,reason",
    [
        ("missing", "INCOMPLETE_ROAD_COVERAGE"),
        ("mapping", "ROAD_MAPPING_UNAVAILABLE"),
        ("mixed", "CONFLICTING_ROAD_GRADES"),
        ("stale", "STALE_DATA"),
        ("future", "INVALID_OBSERVATION_TIME"),
        ("no_time", "NO_OBSERVATION_TIME"),
        ("no_grade", "NO_SOURCE_GRADE"),
    ],
)
def test_no_confirmed_grade_without_complete_fresh_consistent_source(mode, reason):
    links = {"a", "b"}
    data = parse_grades(sections(("a", "원활"), ("b", "원활")))
    if mode == "missing":
        del data["b"]
    elif mode == "mapping":
        links.add(None)
    else:
        data["b"] = {
            "mixed": RoadGrade("정체", NOW, SOURCE),
            "stale": RoadGrade("원활", NOW - timedelta(minutes=31), SOURCE),
            "future": RoadGrade("원활", NOW + timedelta(seconds=1), SOURCE),
            "no_time": RoadGrade("원활", None, SOURCE),
            "no_grade": RoadGrade(None, NOW, SOURCE, "NO_SOURCE_GRADE"),
        }[mode]
    result = consensus(links, data, now=NOW)
    assert result.grade is None and result.reason == reason


def configured():
    repo, provider = AsyncMock(), AsyncMock()
    repo.areas.return_value = [area()]
    repo.parking_counts.return_value = ({"dong": 2}, NOW)
    repo.area_roads.return_value = {"dong": {"a", "b"}}
    repo.nearby_roads.return_value = {"a"}
    repo.hotspots.return_value = [("POI001", "명소")]
    provider.fetch_sections.return_value = sections(("a", "원활"), ("b", "정체"))
    return RecommendationGeographyService(repo, provider), repo, provider


def test_area_and_candidate_scopes_remain_distinct_and_request_reads_reused():
    async def run():
        service, repo, provider = configured()
        a = (await service.areas(CENTER))[0]
        assert a.registered_parking_count == 2
        assert a.congestion == "확인 불가"  # Two different road grades across this dong.
        traffic = await service.traffic(CENTER)
        assert traffic.grade == "원활"  # Only source link a is within the destination radius.
        assert traffic.observed_at == NOW
        provider.fetch_sections.assert_awaited_once()
        repo.nearby_roads.assert_awaited_once_with(CENTER, 100)

    asyncio.run(run())


def test_optional_failure_keeps_boundaries_and_unknown_is_not_zero():
    async def run():
        service, repo, provider = configured()
        repo.parking_counts.side_effect = SQLAlchemyError("database-private")
        provider.fetch_sections.side_effect = ServiceError("RATE_LIMITED", "provider-private")
        a = (await service.areas(CENTER))[0]
        assert a.registered_parking_count is None and a.parking_checked_at is None
        assert a.congestion == "확인 불가"
        assert (await service.traffic(CENTER)).reason == "RATE_LIMITED"
        provider.fetch_sections.assert_awaited_once()
        repo.nearby_roads.side_effect = SQLAlchemyError("database-private")
        assert (await service.traffic(CENTER)).reason == "DATA_UNAVAILABLE"

    asyncio.run(run())


def test_zero_parking_budget_and_newer_conflicting_observations():
    async def run():
        service, repo, provider = configured()
        repo.parking_counts.return_value = ({}, NOW)
        repo.hotspots.side_effect = [[(f"POI{i}", "명소")] for i in range(20)]
        assert (await service.areas(CENTER))[0].registered_parking_count == 0
        provider.fetch_sections.return_value = sections(("a", "정체"), at=NOW.isoformat())
        assert (await service.traffic(CENTER)).reason == "CONFLICTING_ROAD_GRADES"
        provider.fetch_sections.return_value = sections(
            ("a", "서행"),
            at=(NOW + timedelta(microseconds=1)).isoformat(),
        )
        assert (await service.traffic(CENTER)).grade == "서행"
        for _ in range(12):
            await service.traffic(CENTER)
        assert provider.fetch_sections.await_count == service.MAX_HOTSPOTS

    asyncio.run(run())


def test_total_source_time_budget_does_not_fail_recommendations():
    async def run():
        service, _, provider = configured()
        service.FETCH_BUDGET_SECONDS = 0.001

        async def slow(*args):
            await asyncio.sleep(1)

        provider.fetch_sections.side_effect = slow
        assert (await service.traffic(CENTER)).reason == "PROVIDER_TIMEOUT"
        assert (await service.traffic(CENTER)).reason == "PROVIDER_TIMEOUT"
        provider.fetch_sections.assert_awaited_once()

    asyncio.run(run())


def test_provider_validates_hotspot_identity_and_absent_key_without_http():
    async def run():
        calls = []

        def respond(req):
            calls.append(req)
            return httpx.Response(
                200,
                json={
                    "RESULT": {"RESULT.CODE": "INFO-000", "RESULT.MESSAGE": "정상"},
                    "CITYDATA": {
                        "AREA_CD": "OTHER",
                        "AREA_NM": "다른 명소",
                        **sections(("a", "원활")),
                    },
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            for key, code in [("", "SERVICE_UNAVAILABLE"), ("test-key", "PROVIDER_ERROR")]:
                provider = SeoulAreaExtras(http, key)
                with pytest.raises(ServiceError) as exc:
                    await provider.fetch_sections("POI001", "명소", ("ROAD_TRAFFIC_STTS",))
                assert exc.value.code == code
            assert len(calls) == 1

    asyncio.run(run())


def test_engine_refreshes_leftovers_and_original_retry_without_changing_saved_result():
    async def run():
        service, source, engine = setup()
        geo = AsyncMock()
        geo.areas.return_value = [area()]
        geo.traffic.return_value = RoadGrade("서행", NOW, SOURCE, None)
        engine.area_provider = engine.traffic_provider = geo
        req = request()
        first = await service.execute(req)
        assert first.failure is None
        snapshot = first.response.result
        assert "polygons" not in snapshot["search_scope"]["areas"][0]
        assert snapshot["cards"][0]["reason"]["evidence"][0]["information"]["value"] == {
            "grade": "서행"
        }
        geo.traffic.return_value = RoadGrade(reason="ROAD_MAPPING_UNAVAILABLE")
        second_req = next_request(req, first)
        second = await service.execute(second_req)
        assert source.search.await_count == 1
        fact = second.response.result["cards"][0]["reason"]["evidence"][0]["information"]
        assert fact["value"] is None and fact["reason_code"] == "ROAD_MAPPING_UNAVAILABLE"
        assert first.response.result == snapshot
        from tests.unit.recommendation.test_progress import candidate

        original = candidate("retry-original")
        original.congestion, original.congestion_source = "원활", "old"
        original.congestion_observed_at = NOW
        retried = await service.retry_routes(next_request(second_req, second), [original])
        assert (
            retried.response.result["cards"][0]["reason"]["evidence"][0]["information"]["value"]
            is None
        )
        assert original.congestion == "원활"
        assert source.search.await_count == 1

    asyncio.run(run())
