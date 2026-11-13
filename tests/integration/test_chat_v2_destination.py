"""Destination search is independent of origin; actual PostGIS, synthetic providers."""

import asyncio

import httpx
import pytest
from sqlalchemy import func, select, update

from app.models import AdministrativeArea, ChatResult, RecommendationResult, RegionBoundary
from app.modules.chat_v2.destination import resolve_destination
from tests.integration.test_chat_v2_dialogue import Harness

pytestmark = pytest.mark.integration


class RegionalHarness(Harness):
    def __init__(self, url):
        super().__init__(url)
        self.requests = []

    async def start(self):
        await super().start()
        async with self.writers.begin() as db:
            await db.execute(
                update(AdministrativeArea)
                .where(AdministrativeArea.dong_code == "dong")
                .values(name="성수1가1동")
            )
            area = await db.get(AdministrativeArea, "dong")
            for code, name in [("11", "서울특별시"), ("11010", "서울특별시 성동구")]:
                db.add(
                    RegionBoundary(
                        code_system="sgis",
                        region_code=code,
                        name=name,
                        boundary_version="test",
                        geometry=area.geometry,
                        source="sgis_areas",
                        processor_version="test",
                        processed_at=self.observed,
                    )
                )

    def respond(self, req):
        self.requests.append((req.url.path, dict(req.url.params)))
        if req.url.params.get("query") == "수원역":
            return httpx.Response(
                200,
                json={
                    "documents": [
                        {
                            "id": "suwon",
                            "place_name": "수원역",
                            "category_group_code": "SW8",
                            "category_name": "지하철역",
                            "address_name": "경기도 수원시 수원역",
                            "road_address_name": "경기도 수원시 수원역",
                            "x": "127.0",
                            "y": "37.26",
                        }
                    ],
                    "meta": {"is_end": True},
                },
            )
        if req.url.path.endswith("coord2regioncode.json") and req.url.params.get("y") == "37.26":
            return httpx.Response(
                200,
                json={
                    "documents": [
                        {
                            "region_type": "B",
                            "region_1depth_name": "경기도",
                            "region_2depth_name": "수원시",
                        }
                    ]
                },
            )
        response = super().respond(req)
        if req.url.path.endswith("keyword.json") and "성수" in req.url.params.get("query", ""):
            body = response.json()
            if body["documents"]:
                body["documents"].insert(
                    0,
                    {
                        **body["documents"][0],
                        "id": "outside",
                        "x": "126.0",
                        "y": "37.0",
                    },
                )
            return httpx.Response(response.status_code, json=body)
        return response


async def regional(h):
    return await h.say(
        "성수 카페 추천해줘",
        {
            "kind": "recommend",
            "search_area_query": "성수",
            "purposes": [{"purpose": "cafe"}],
        },
    )


def test_regional_recommendation_then_later_driving_preserves_search_area(normalized_database):
    async def run():
        h = RegionalHarness(normalized_database)
        try:
            await h.start()
            district = await resolve_destination(h.readers, "성동구")
            assert district.area_ids == ["dong"]
            first = await regional(h)
            assert first.response.status == "succeeded", first.response.model_dump()
            published = first.response.recommendation
            assert published.selection_mode == "regional" and published.search_area.label == "성수"
            assert [c.place.value.place_id for c in published.cards] == ["kakao:cafe-1"]
            assert published.cards[0].route.value is None
            assert published.cards[0].route.reason_code == "ORIGIN_NOT_SET"
            assert not any(path == "/v1/directions" for path, _ in h.requests)
            current = await h.current()
            assert current.conditions.origin is None and current.search_area.area_ids == ["dong"]
            before_calls = len(h.requests)
            why = await h.say(
                "그곳 왜 추천했어",
                {
                    "kind": "read",
                    "topics": ["recommendation_reason"],
                    "targets": [{"kind": "focus"}],
                },
                viewed={"result_id": published.result_id},
            )
            assert why.response.lookup.items[0].data["selection_mode"] == "regional"
            assert published.cards[0].place.value.name in why.response.answer
            assert len(h.requests) == before_calls
            asked = await h.say(
                "그곳까지 얼마나 걸려",
                {
                    "kind": "read",
                    "topics": ["driving"],
                    "targets": [{"kind": "focus"}],
                },
            )
            assert asked.response.clarification.required_field == "lookup_origin"
            candidates = await h.say(
                "수원역",
                {
                    "kind": "read",
                    "origin_query": "수원역",
                    "topics": ["driving"],
                    "targets": [{"kind": "focus"}],
                },
            )
            assert len(candidates.response.origin_options) == 1
            pending = (await h.current()).pending_read
            stale_choice = await h.say(
                "맞아",
                {"kind": "select_origin", "position": 1, "for_lookup": True},
                stale=current,
            )
            assert stale_choice.response.error.code == "CONFLICT"
            answered = await h.say(
                "맞아",
                {
                    "kind": "select_origin",
                    "position": 1,
                    "for_lookup": True,
                },
                reply=pending.clarification_id,
            )
            assert answered.response.lookup.items[0].data["origin_label"] == "수원역"
            assert (
                answered.response.lookup.items[0].data["route"]["value"]["duration_seconds"] == 600
            )
            assert (await h.current()).conditions.origin is None
            assert (await h.current()).search_area == current.search_area
            h.place_id = "cafe-next"
            again = await h.say("다른 곳", {"kind": "recommend"})
            assert again.response.recommendation.search_area.label == "성수"
            assert again.response.condition_version == current.condition_version
            async with h.writers() as db:
                assert await db.scalar(select(func.count()).select_from(ChatResult)) == 2
                assert await db.scalar(select(func.count()).select_from(RecommendationResult)) == 0
        finally:
            await h.close()

    asyncio.run(run())


def test_separate_origin_and_destination_with_travel_limit(normalized_database):
    async def run():
        h = RegionalHarness(normalized_database)
        try:
            await h.start()
            asked = await h.say(
                "수원역에서 출발해서 성수 카페 30분 이내 추천",
                {
                    "kind": "recommend",
                    "origin_query": "수원역",
                    "search_area_query": "성수",
                    "purposes": [{"purpose": "cafe"}],
                    "max_travel_seconds": 1800,
                },
            )
            assert asked.response.clarification.required_field == "origin"
            assert (await h.current()).conditions.origin is None
            result = await h.say("맞아", {"kind": "select_origin", "position": 1})
            assert result.response.status == "succeeded", result.response.model_dump()
            assert result.response.conditions.origin.label == "수원역"
            assert result.response.recommendation.search_area.label == "성수"
            assert result.response.recommendation.selection_mode == "driving"
            queries = [
                args
                for path, args in h.requests
                if path.endswith("keyword.json") and args["query"] != "수원역"
            ]
            assert queries and all("성수" in args["query"] for args in queries)
            assert all(float(args["y"]) > 37.5 for args in queries)
            routes = [args for path, args in h.requests if path == "/v1/directions"]
            assert routes and all("37.26" in args["origin"] for args in routes)
            assert all(
                c.place.value.place_id != "kakao:outside"
                for c in result.response.recommendation.cards
            )
        finally:
            await h.close()

    asyncio.run(run())


def test_unresolved_area_does_not_fall_back_to_previous_area(normalized_database):
    async def run():
        h = RegionalHarness(normalized_database)
        try:
            await h.start()
            await regional(h)
            before = await h.current()
            calls = len(h.requests)
            unknown = await h.say(
                "없는동 카페 추천",
                {
                    "kind": "recommend",
                    "search_area_query": "없는동",
                    "purposes": [{"purpose": "cafe"}],
                },
            )
            assert unknown.response.clarification.required_field == "search_area"
            assert len(h.requests) == calls + 1  # One bounded external identity lookup.
            calls = len(h.requests)
            again = await h.say("다른 곳", {"kind": "recommend"})
            assert again.response.clarification.required_field == "search_area"
            assert len(h.requests) == calls
            assert (await h.current()).search_area == before.search_area
            assert (await h.current()).condition_version == before.condition_version
        finally:
            await h.close()

    asyncio.run(run())
