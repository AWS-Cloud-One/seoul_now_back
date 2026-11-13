"""Real PostGIS, synthetic provider: scope, ambiguity, ownership and legacy recovery."""

import asyncio

import httpx
import pytest
from sqlalchemy import update

from app.models import PopulationArea
from app.modules.chat_v2.region_contracts import DestinationArea
from app.modules.chat_v2.region_resolution import resolve_region
from app.modules.chat_v2.runtime import ChatRuntime
from app.modules.chat_v2.storage import ChatV2Store
from app.modules.chat_v2.storage_contracts import StorageRejected
from tests.integration.test_chat_v2_client_contracts import choose
from tests.integration.test_chat_v2_destination import RegionalHarness
from tests.integration.test_chat_v2_lifecycle import run_request

pytestmark = pytest.mark.integration


class RegionHarness(RegionalHarness):
    async def start(self):
        await super().start()
        async with self.writers.begin() as db:
            original = await db.get(PopulationArea, "POI001")
            for code, name in [("POI007", "홍대 관광특구"), ("POI055", "홍대입구역(2호선)")]:
                db.add(
                    PopulationArea(
                        area_code=code,
                        name=name,
                        geometry=original.geometry,
                        source=original.source,
                        source_version="v1",
                        processed_at=self.observed,
                        processor_version="test",
                    )
                )

    def respond(self, req):
        if req.url.path.endswith("keyword.json") and not req.url.params.get("x"):
            self.requests.append((req.url.path, dict(req.url.params)))
            query = req.url.params["query"]
            if query == "장애":
                return httpx.Response(503)
            names = {
                "합정역": ["합정역"],
                "서울역": ["서울역"],
                "홍대입구역(공항철도)": ["홍대입구역 공항철도"],
                "애매한곳": ["검증역", "검증대학교"],
                "수원역": ["수원역"],
                "홍익대학교 정문": ["홍익대학교 정문"],
            }.get(query, [])
            rows = [
                {
                    "id": str(i),
                    "place_name": name,
                    "x": "126.98",
                    "y": "37.57",
                    "address_name": "서울 중구 검증로",
                    "category_group_code": "SW8",
                    "category_name": "지하철역",
                }
                for i, name in enumerate(names)
            ]
            if query == "수원역":
                rows[0]["y"] = "37.26"
            return httpx.Response(200, json={"documents": rows, "meta": {"is_end": True}})
        response = super().respond(req)
        if req.url.path.endswith("keyword.json") and req.url.params.get("x"):
            body = response.json()
            if body["documents"]:
                body["documents"].extend(
                    [
                        {**body["documents"][0], "id": "outside", "x": "128.0"},
                        {**body["documents"][0], "id": "beyond-radius", "x": "126.988"},
                    ]
                )
            return httpx.Response(200, json=body)
        return response


def test_official_and_radius_scopes_keep_conditions_and_restore(normalized_database):
    async def run():
        h = RegionHarness(normalized_database)
        try:
            await h.start()
            assert DestinationArea(label="성수", area_ids=["dong"]).kind == "administrative"
            first = await h.say(
                "홍대로 가고 싶어", {"kind": "recommend", "search_area_query": "홍대"}
            )
            assert first.response.clarification.required_field == "purposes"
            assert (await h.current()).draft.search_area.boundary_ids == ["POI007"]
            result = await h.say(
                "셋이 카페",
                {"kind": "recommend", "headcount": 3, "purposes": [{"purpose": "cafe"}]},
            )
            rec = result.response.recommendation
            assert rec and rec.search_area.kind == "official", result.response
            assert rec.search_area.area_ids == [] and rec.effective_conditions.origin is None
            assert "kakao:outside" not in [c.place.value.place_id for c in rec.cards]
            assert any("rect" in q and q["query"] == "카페" for _, q in h.requests)
            h.place_id = "cafe-near"
            nearby = await h.say(
                "합정역 500m",
                {"kind": "recommend", "search_area_query": "합정역", "search_radius_m": 500},
            )
            rec = nearby.response.recommendation
            assert rec and rec.search_area.kind == "radius", nearby.response
            assert [c.place.value.place_id for c in rec.cards] == ["kakao:cafe-near"]
            assert (
                rec.effective_conditions.headcount == 3
                and rec.effective_conditions.purposes[0].purpose == "cafe"
            )
            assert any(q.get("radius") == "500" for _, q in h.requests)
            restored = await ChatV2Store(h.writers).conversation(
                h.credential, h.conversation.conversation_id
            )
            assert restored.search_area == rec.search_area
            h.place_id = "another"
            another = await h.say("다른 곳", {"kind": "recommend"})
            assert another.response.recommendation.search_area == rec.search_area
            h.place_id = "wide"
            wider = await h.say("1km로", {"kind": "recommend", "search_radius_m": 1000})
            assert wider.response.recommendation.search_area.anchor == rec.search_area.anchor
            assert wider.response.recommendation.search_area.radius_m == 1000
            h.place_id = "with-driving"
            asked = await h.say(
                "서울역에서 홍대 카페",
                {"kind": "recommend", "search_area_query": "홍대", "origin_query": "서울역"},
            )
            assert asked.question.required_field == "origin"
            driven = await h.say("첫번째", {"kind": "select_origin", "position": 1})
            assert driven.response.recommendation.selection_mode == "driving"
            assert driven.response.recommendation.search_area.kind == "official"
        finally:
            await h.close()

    asyncio.run(run())


def test_region_choices_restore_skip_model_and_reject_stale(normalized_database):
    async def run():
        h = RegionHarness(normalized_database)
        rt = ChatRuntime(h.store, h.execution)
        try:
            await h.start()
            asked = await h.say(
                "애매한곳 카페",
                {
                    "kind": "recommend",
                    "search_area_query": "애매한곳",
                    "purposes": [{"purpose": "cafe"}],
                    "scope": "request_only",
                },
            )
            assert len(asked.question.options) == 2
            before = await h.current()
            restored = await ChatV2Store(h.writers).conversation(
                h.credential, h.conversation.conversation_id
            )
            assert len(restored.draft.destination_options) == 2
            assert restored.draft.scope == "request_only" and restored.search_area is None
            stale = asked.question.options[1]
            calls = len(h.requests)
            h.model.interpret.reset_mock()
            done = await run_request(h, rt, choose(asked.question.options[0]))
            h.model.interpret.assert_not_awaited()
            assert done.response.recommendation.scope == "request_only"
            assert done.response.recommendation.search_area.anchor.name == "검증역"
            assert (await h.current()).conditions == before.conditions
            assert all(q.get("query") != "애매한곳" for _, q in h.requests[calls:])
            with pytest.raises(StorageRejected, match="ACTION_STALE"):
                await rt.submit(h.credential, h.conversation.conversation_id, choose(stale))
        finally:
            await rt.close()
            await h.close()

    asyncio.run(run())


def test_resolution_errors_and_strict_inside_never_widen(normalized_database):
    async def run():
        h = RegionHarness(normalized_database)
        try:
            await h.start()
            qualified = await resolve_region(
                h.readers, h.execution.tools.kakao, "홍대입구역(공항철도)"
            )
            assert qualified.region.kind == "radius"
            assert qualified.region.anchor.name == "홍대입구역 공항철도"
            for query, status in [
                ("없는장소", "not_found"),
                ("장애", "unavailable"),
                ("수원역", "out_of_scope"),
                ("홍익대학교 정문", "resolved"),
            ]:
                result = await resolve_region(h.readers, h.execution.tools.kakao, query)
                assert result.status == status, result
            assert (
                await resolve_region(h.readers, h.execution.tools.kakao, "홍대", radius=6000)
            ).status == "needs_input"
            assert (
                await resolve_region(
                    h.readers, h.execution.tools.kakao, "서울숲", relation="inside"
                )
            ).status == "needs_input"
            async with h.writers.begin() as db:
                await db.execute(
                    update(PopulationArea)
                    .where(PopulationArea.area_code == "POI007")
                    .values(is_active=False)
                )
            assert (
                await resolve_region(h.readers, h.execution.tools.kakao, "홍대")
            ).status == "unavailable"
            # Not silently converted to a campus polygon or the previous area.
            first = await h.say(
                "홍익대학교 정문 안 카페",
                {
                    "kind": "recommend",
                    "search_area_query": "홍익대학교 정문",
                    "spatial_relation": "inside",
                    "purposes": [{"purpose": "cafe"}],
                },
            )
            assert first.question.required_field == "search_area"
            follow = await h.say("주변으로", {"kind": "recommend", "spatial_relation": "near"})
            assert follow.response.recommendation.search_area.radius_m == 1000
        finally:
            await h.close()

    asyncio.run(run())
