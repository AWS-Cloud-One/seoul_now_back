import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.repositories.map import Observation
from app.services.area_queries import AreaQueryService
from app.services.current_place_answers import CurrentPlaceAnswers
from tests.unit.services.test_place_details import scenario


@pytest.mark.parametrize(
    "mode,reason",
    [
        ("fresh", None),
        ("stale", "STALE_DATA"),
        ("expired", "STALE_DATA"),
        ("future", "SOURCE_TIME_UNKNOWN"),
        ("no_time", "SOURCE_TIME_UNKNOWN"),
        ("replacement", "REPLACEMENT_VALUE"),
        ("missing", "NO_RECORD"),
        ("collection_failed", "COLLECTION_FAILED"),
        ("version", "GEOMETRY_VERSION_MISMATCH"),
        ("outside", "OUTSIDE_OBSERVATION_AREA"),
        ("unprepared", "DATA_UNAVAILABLE"),
    ],
)
def test_population_answers_preserve_quality_time_and_scope(mode, reason):
    async def run():
        env = await scenario()
        now = datetime.now(UTC)
        at = now - timedelta(minutes={"stale": 90, "expired": 121, "future": -1}.get(mode, 1))
        if mode == "no_time":
            at = None
        row = (
            Observation(
                {
                    "min_people": 0,
                    "max_people": 10,
                    "congestion": "normal",
                    "is_replacement": mode == "replacement",
                },
                at,
                now,
                mode == "collection_failed",
            )
            if mode != "missing"
            else None
        )
        repo, extras = AsyncMock(), AsyncMock()
        repo.populations_at.return_value = (
            None
            if mode == "unprepared"
            else []
            if mode == "outside"
            else [
                {
                    "area_code": "POI001",
                    "name": "관측 구역",
                    "boundary_version": "v1",
                    "reason_code": reason if mode == "version" else None,
                    "observation": row,
                }
            ]
        )
        areas = AreaQueryService(repo, extras)
        request = SimpleNamespace(
            target=SimpleNamespace(
                place_id="kakao:original",
                result_id=env.req.result_id,
            )
        )
        answer = await CurrentPlaceAnswers(env.detail, env.travel, areas).answer(
            request,
            "population",
            env.stored,
        )
        fact = answer.information[0]
        assert fact.reason_code == reason
        if mode in {"outside", "unprepared"}:
            assert fact.value is None and fact.status == "unavailable"
            assert ("구역 밖" in answer.text) == (mode == "outside")
        else:
            assert fact.value["area_code"] == "POI001"
            assert fact.value["scope"] == "population_observation_area"
            assert fact.observed_at == (
                None if mode in {"future", "missing", "no_time", "version"} else at
            )
            if mode in {"fresh", "stale", "collection_failed"}:
                assert fact.value["population"]["min_people"] == 0
                assert "0~10명" in answer.text
                assert ("오래된 관측" in answer.text) == (mode == "stale")
                assert ("최근 수집에 실패" in answer.text) == (mode == "collection_failed")
            else:
                assert fact.value["population"] is None and "0~10명" not in answer.text
            assert "매장 내부" in answer.text
        extras.fetch.assert_not_awaited()
        repo.read_section.assert_not_awaited()
        env.parking.search.assert_not_awaited()
        env.queries._driving.assert_not_awaited()

    asyncio.run(run())


def test_overlapping_observation_areas_are_never_added_or_averaged():
    async def run():
        env = await scenario()
        now = datetime.now(UTC)
        repo = AsyncMock()
        repo.populations_at.return_value = [
            {
                "area_code": code,
                "name": name,
                "boundary_version": "v1",
                "reason_code": None,
                "observation": Observation(
                    {
                        "min_people": count,
                        "max_people": count,
                        "congestion": "normal",
                        "is_replacement": False,
                    },
                    now,
                    now,
                    False,
                ),
            }
            for code, name, count in [("POI001", "첫 구역", 100), ("POI002", "둘째 구역", 900)]
        ]
        request = SimpleNamespace(
            target=SimpleNamespace(place_id="kakao:original", result_id=env.req.result_id)
        )
        answer = await CurrentPlaceAnswers(env.detail, env.travel, AreaQueryService(repo)).answer(
            request, "population", env.stored
        )
        assert [item.value["population"]["min_people"] for item in answer.information] == [100, 900]
        assert "첫 구역(POI001)" in answer.text and "둘째 구역(POI002)" in answer.text
        assert "500명" not in answer.text and "1,000명" not in answer.text

    asyncio.run(run())
