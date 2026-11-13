"""P2 conversation paths with real storage/services; LLM/provider responses are synthetic."""

import asyncio
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import Settings
from app.db.session import read_session_factory, write_session_factory
from app.models import ChatRunCommit, MapLayerVersion
from app.modules.chat_v2.contracts import DialogueProposal, RunRequest
from app.modules.chat_v2.execution import build_dialogue_execution
from app.modules.chat_v2.storage import ChatV2Store
from app.modules.chat_v2.storage_contracts import StorageRejected
from app.services.driving_requests import DrivingRequests
from tests.integration.test_recommendation_geography import seed
from tests.unit.recommendation.test_geography import sections
from tests.unit.services.test_part1_queries import route_body

pytestmark = pytest.mark.integration


class Harness:
    def __init__(self, url):
        self.engine = create_async_engine(url)
        self.writers = write_session_factory(self.engine)
        self.readers = read_session_factory(self.engine)
        self.store = ChatV2Store(self.writers)
        self.model = AsyncMock()
        self.mode, self.place_id, self.seconds = "ok", "cafe-1", 600
        self.calls = []
        self.cache_time = 0
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(self.respond))
        self.settings = Settings(
            _env_file=None,
            database_url=url,
            kakao_rest_api_key="synthetic",
            seoul_citydata_api_key="synthetic",
        )
        self.execution = build_dialogue_execution(
            self.store,
            self.model,
            self.readers,
            self.http,
            self.settings,
            driving_requests=DrivingRequests(budget=100, interval=0, clock=lambda: self.cache_time),
        )

    async def start(self):
        async with self.writers.begin() as db:
            self.observed = await seed(db)
        self.credential = await self.store.create_session()
        self.conversation = await self.store.create_conversation(self.credential)

    async def close(self):
        await self.http.aclose()
        await self.engine.dispose()

    def respond(self, req):
        self.calls.append(req.url.path)
        if req.url.path.endswith("coord2regioncode.json"):
            return httpx.Response(
                200,
                json={
                    "documents": [
                        {
                            "region_type": "B",
                            "region_1depth_name": "서울특별시",
                            "region_2depth_name": "중구",
                        }
                    ]
                },
            )
        if req.url.path.endswith("keyword.json"):
            if self.mode == "search_error":
                return httpx.Response(503, json={"private": "synthetic"})
            origin = req.url.params.get("query") == "서울역"
            rows = (
                []
                if self.mode == "empty"
                else [
                    {
                        "id": "station" if origin else self.place_id,
                        "place_name": "서울역" if origin else "검증 카페",
                        "category_group_code": "SW8" if origin else "CE7",
                        "category_name": "지하철역" if origin else "카페",
                        "address_name": "서울특별시 중구 검증로",
                        "road_address_name": "서울특별시 중구 검증로",
                        "x": "126.98",
                        "y": "37.57",
                    }
                ]
            )
            if self.mode == "two" and rows:
                rows.append({**rows[0], "id": "cafe-2", "place_name": "다른 카페"})
            return httpx.Response(200, json={"documents": rows, "meta": {"is_end": True}})
        if req.url.path == "/v1/directions":
            if self.mode == "route_error":
                raise httpx.ReadTimeout("synthetic", request=req)
            body = route_body()
            body["routes"][0]["summary"]["duration"] = self.seconds
            return httpx.Response(200, json=body)
        if req.url.host == "openapi.seoul.go.kr":
            return httpx.Response(
                200,
                json={
                    "RESULT": {"CODE": "INFO-000"},
                    "CITYDATA": {
                        "AREA_CD": "POI001",
                        "AREA_NM": "명소",
                        **sections(
                            ("1000001401", "서행"),
                            ("1000000301", "서행"),
                            at=self.observed.isoformat(),
                        ),
                    },
                },
            )
        if req.url.path.endswith("address.json"):
            return httpx.Response(200, json={"documents": [], "meta": {"is_end": True}})
        raise AssertionError("Unexpected HTTP call")

    async def current(self):
        return await self.store.conversation(self.credential, self.conversation.conversation_id)

    async def say(self, text, decision, *, viewed=None, stale=None, reply=None):
        self.cache_time += 61
        current = stale or await self.current()
        if decision["kind"] not in {"needs_input", "unsupported"}:
            decision = {**decision, "evidence_text": text}
        self.model.interpret.return_value = DialogueProposal(decision=decision)
        request = RunRequest(
            request_id=uuid4(),
            expected_condition_version=current.condition_version,
            expected_state_version=current.state_version,
            input={"text": text, "reply_to_clarification_id": reply},
            viewed_context=viewed,
        )
        return await self.execution.execute(self.credential, current.conversation_id, request)

    async def first_recommendation(self):
        asked = await self.say(
            "서울역에서 30분 이내 카페 추천",
            {
                "kind": "recommend",
                "origin_query": "서울역",
                "purposes": [{"purpose": "cafe"}],
                "max_travel_seconds": 1800,
            },
        )
        assert asked.response.kind == "clarification" and len(asked.response.origin_options) == 1
        before = await self.current()
        assert before.condition_version == 0 and before.conditions.origin is None
        done = await self.say(
            "맞아",
            {"kind": "select_origin", "position": 1},
            reply=before.pending_question.clarification_id,
        )
        assert done.response.status == "succeeded", done.response.model_dump()
        assert done.response.recommendation is not None
        return done


def test_first_recommendation_reads_and_interruption_keep_pending_question(normalized_database):
    async def run():
        h = Harness(normalized_database)
        try:
            await h.start()
            first = await h.first_recommendation()
            result_id = first.response.recommendation.result_id
            state = await h.current()
            assert state.conditions.headcount is None and state.condition_version == 1
            await h.say(
                "인원을 늘려줘",
                {"kind": "needs_input", "required_field": "headcount", "question": "몇 명인가요?"},
            )
            pending = (await h.current()).pending_question
            h.seconds = 1200
            read = await h.say(
                "그곳 주차랑 지금 얼마나 걸려",
                {
                    "kind": "read",
                    "topics": ["parking", "driving"],
                    "targets": [{"kind": "position", "position": 1}],
                },
                viewed={"result_id": result_id},
            )
            assert read.response.status == "succeeded"
            assert read.response.lookup.items[0].data["total_count"] == 1
            assert read.response.lookup.items[1].data["route"]["value"]["duration_seconds"] == 1200
            assert (await h.current()).pending_question == pending
            before_calls = len(h.calls)
            reason = await h.say(
                "아까 왜 추천했어",
                {
                    "kind": "read",
                    "topics": ["recommendation_reason"],
                    "targets": [{"kind": "focus"}],
                },
                viewed={"result_id": result_id},
            )
            assert len(h.calls) == before_calls
            old = reason.response.lookup.items[0].data
            assert old["conditions"]["headcount"] is None
            eta = next(e for e in old["reason"]["evidence"] if e["type"] == "eta")
            assert eta["information"]["value"]["duration_seconds"] == 600
            h.place_id = "cafe-3"
            changed = await h.say(
                "셋이요",
                {"kind": "change_headcount", "headcount": 3, "scope": "persistent"},
                reply=pending.clarification_id,
            )
            assert (
                changed.response.conditions.headcount == 3
                and changed.response.condition_version == 2
            )
            assert (await h.current()).pending_question is None
            # A pure recommendation creates a receipt/result at the SAME condition version.
            h.place_id = "cafe-4"
            repeat = await h.say("다른 곳 추천", {"kind": "recommend"})
            assert repeat.response.condition_version == 2
            assert repeat.response.snapshot.finalization.commit.receipt.changed_fields == []
            assert repeat.response.recommendation is not None
            old_read = await h.say(
                "첫 번째 주소",
                {
                    "kind": "read",
                    "topics": ["place_info"],
                    "targets": [{"kind": "position", "position": 1}],
                },
                viewed={"result_id": result_id},
                stale=state,
            )
            assert old_read.response.lookup.items[0].data["place_id"] == "kakao:cafe-1"
            rejected = await h.say(
                "4명으로",
                {"kind": "change_headcount", "headcount": 4, "scope": "persistent"},
                stale=state,
            )
            assert rejected.response.error.code == "CONFLICT"
            assert (await h.current()).conditions.headcount == 3
            async with h.writers() as db:
                assert await db.scalar(select(func.count()).select_from(ChatRunCommit)) == 3
        finally:
            await h.close()

    asyncio.run(run())


def test_search_reference_ambiguity_and_cross_session_are_safe(normalized_database):
    async def run():
        h = Harness(normalized_database)
        try:
            await h.start()
            h.mode = "two"
            searched = await h.say(
                "성수 카페 찾아줘", {"kind": "search_places", "query": "성수 카페"}
            )
            assert len(searched.response.lookup.places) == 2
            assert (await h.current()).conditions.origin is None
            ambiguous = await h.say(
                "거기 주소",
                {"kind": "read", "topics": ["place_info"], "targets": [{"kind": "focus"}]},
            )
            assert ambiguous.response.kind == "clarification"
            pending = (await h.current()).pending_read
            chosen = await h.say(
                "두 번째",
                {
                    "kind": "read",
                    "topics": ["place_info"],
                    "targets": [{"kind": "position", "position": 2}],
                },
                viewed={"collection_id": searched.identity.run_id},
                reply=pending.clarification_id,
            )
            assert chosen.response.lookup.items[0].data["name"] == "다른 카페"
            assert (await h.current()).pending_read is None
            no_reason = await h.say(
                "왜 추천했어",
                {
                    "kind": "read",
                    "topics": ["recommendation_reason"],
                    "targets": [{"kind": "focus"}],
                },
            )
            assert no_reason.response.lookup.items[0].reason_code == "NOT_A_RECOMMENDATION"
            calls = h.model.interpret.await_count
            other = await h.store.create_session()
            current = await h.current()
            with pytest.raises(StorageRejected, match="NOT_FOUND"):
                await h.execution.execute(
                    other,
                    current.conversation_id,
                    RunRequest(
                        request_id=uuid4(),
                        expected_condition_version=0,
                        expected_state_version=0,
                        input={"text": "주소"},
                    ),
                )
            with pytest.raises(StorageRejected, match="NOT_FOUND"):
                await h.say(
                    "주소",
                    {"kind": "read", "topics": ["place_info"], "targets": [{"kind": "focus"}]},
                    viewed={"result_id": uuid4()},
                )
            assert h.model.interpret.await_count == calls
        finally:
            await h.close()

    asyncio.run(run())


def test_lookup_empty_missing_data_and_provider_error_are_distinct(normalized_database):
    async def run():
        h = Harness(normalized_database)
        try:
            await h.start()
            h.mode = "empty"
            empty = await h.say("없는 카페", {"kind": "search_places", "query": "없는 카페"})
            assert empty.response.lookup.outcome == "empty"
            h.mode = "search_error"
            failed = await h.say("검색", {"kind": "search_places", "query": "카페"})
            assert failed.response.status == "failed" and failed.response.clarification is None
            h.mode = "ok"
            recommended = await h.first_recommendation()
            h.mode = "route_error"
            partial = await h.say(
                "주소랑 현재 이동시간",
                {
                    "kind": "read",
                    "topics": ["place_info", "driving"],
                    "targets": [{"kind": "focus"}],
                },
            )
            assert partial.response.lookup.outcome == "partial"
            assert [i.status for i in partial.response.lookup.items] == ["ok", "error"]
            async with h.writers.begin() as db:
                await db.execute(
                    update(MapLayerVersion)
                    .where(MapLayerVersion.layer == "parking")
                    .values(is_ready=False)
                )
            missing = await h.say(
                "주차", {"kind": "read", "topics": ["parking"], "targets": [{"kind": "focus"}]}
            )
            assert missing.response.lookup.outcome == "unavailable"
            assert (await h.current()).condition_version == recommended.response.condition_version
        finally:
            await h.close()

    asyncio.run(run())


def test_required_only_questions_keep_draft_across_independent_search(normalized_database):
    async def run():
        h = Harness(normalized_database)
        try:
            await h.start()
            asked = await h.say("추천해줘", {"kind": "recommend"})
            assert asked.response.clarification.required_field == "purposes"
            before = await h.current()
            assert before.conditions.origin is None and before.draft.conditions.origin is None
            await h.say("성수 카페 찾아줘", {"kind": "search_places", "query": "성수 카페"})
            after = await h.current()
            assert after.draft == before.draft and after.pending_question == before.pending_question
            done = await h.say(
                "카페",
                {"kind": "recommend", "purposes": [{"purpose": "cafe"}]},
                reply=after.pending_question.clarification_id,
            )
            assert done.response.status == "succeeded" and done.response.recommendation is not None
            current = await h.current()
            assert current.conditions.headcount is None and current.conditions.group_type is None
            assert current.draft is None and current.condition_version == 1
        finally:
            await h.close()

    asyncio.run(run())
