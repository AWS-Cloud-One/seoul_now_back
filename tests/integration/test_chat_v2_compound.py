"""F03/F06/F07/F08 on real DB/services, with synthetic model and HTTP inputs."""

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import func, select, update

from app.models import ChatRun, ChatRunCommit, MapFeature, MapLayerVersion
from app.modules.chat_v2 import candidate_conditions
from app.modules.chat_v2.contracts import DialogueProposal
from app.modules.chat_v2.runtime import ChatRuntime
from tests.integration.test_chat_v2_dialogue import Harness
from tests.integration.test_chat_v2_lifecycle import request, run_request

pytestmark = pytest.mark.integration
PARKING = {"terms": [{"kind": "registered_parking_exists", "radius_m": 500}]}
NO_MATCH = {"terms": [{"kind": "candidate_outcome", "outcome": "no_matches"}]}
HAS_MATCH = {"terms": [{"kind": "candidate_outcome", "outcome": "has_verified"}]}


def test_temporary_conditions_and_same_flow_keep_persistent_conditions(normalized_database):
    async def run():
        h = Harness(normalized_database)
        try:
            await h.start()
            await h.first_recommendation()
            before = await h.current()
            first = await h.say(
                "이번만 20분",
                {"kind": "recommend", "scope": "request_only", "max_travel_seconds": 1200},
            )
            assert first.response.recommendation.effective_conditions.max_travel_seconds == 1200, (
                first.model_dump()
            )
            assert first.response.conditions.max_travel_seconds == 1800
            assert first.response.condition_version == before.condition_version
            h.place_id = "cafe-next"
            second = await h.say("같은 조건 다른 곳", {"kind": "recommend", "use_previous": True})
            assert second.response.recommendation.effective_conditions.max_travel_seconds == 1200
            assert second.response.recommendation.flow_id == first.response.recommendation.flow_id
            assert (await h.current()).conditions == before.conditions
            assert (await h.current()).active_flow.excluded == ["kakao:cafe-1", "kakao:cafe-next"]
            reason = await h.say(
                "여기 왜 추천",
                {
                    "kind": "read",
                    "topics": ["recommendation_reason"],
                    "targets": [{"kind": "focus"}],
                },
            )
            assert reason.response.lookup.items[0].data["conditions"]["max_travel_seconds"] == 1200
            h.place_id = "cafe-last"
            older = await h.say(
                "이 조건 다른 곳",
                {"kind": "recommend", "use_previous": True},
                viewed={"result_id": first.response.recommendation.result_id},
            )
            assert older.response.recommendation
            assert (await h.current()).active_flow.excluded == [
                "kakao:cafe-1",
                "kakao:cafe-last",
                "kakao:cafe-next",
            ]
        finally:
            await h.close()

    asyncio.run(run())


def test_exact_radius_and_explicit_fallback_unknown_never_relaxes(normalized_database):
    async def run():
        h = Harness(normalized_database)
        try:
            await h.start()
            # All published facilities are about 666 m away: outside 500, inside 1000.
            async with h.writers.begin() as db:
                await db.execute(
                    update(MapFeature)
                    .where(MapFeature.layer == "parking")
                    .values(geometry=func.ST_GeomFromText("POINT(126.98 37.576)", 4326))
                )
            decision = {
                "kind": "recommend",
                "purposes": [{"purpose": "cafe"}],
                "constraints": PARKING,
                "alternative": {
                    "when": NO_MATCH,
                    "omit_parking": True,
                    "evidence_text": "없으면 빼줘",
                },
            }
            relaxed = await h.say("주차 500m 카페 없으면 빼줘", decision)
            assert relaxed.response.recommendation, relaxed.model_dump()
            assert relaxed.response.evaluation.branch == "alternative"
            assert relaxed.response.evaluation.primary_outcome == "no_candidates"
            assert relaxed.response.recommendation.cards[0].parking.search_radius_meters == 1000
            before = await h.current()
            h.place_id = "cafe-unknown"
            async with h.writers.begin() as db:
                await db.execute(
                    update(MapLayerVersion)
                    .where(MapLayerVersion.layer == "parking")
                    .values(is_ready=False)
                )
            unknown = await h.say(
                "새 후보 있으면 3명 주차 500m 카페 없으면 빼줘",
                {**decision, "headcount": 3, "commit_guard": HAS_MATCH},
            )
            assert (
                unknown.response.kind == "decision"
                and unknown.response.evaluation.guard == "unknown"
            )
            assert unknown.response.evaluation.branch == "primary"
            assert (await h.current()).conditions == before.conditions
        finally:
            await h.close()

    asyncio.run(run())


def test_verified_candidates_survive_unknown_candidates(normalized_database, monkeypatch):
    async def run():
        h = Harness(normalized_database)
        original = candidate_conditions.assess

        async def mixed(session, candidate, predicate):
            result = await original(session, candidate, predicate)
            if candidate.place.place_id == "kakao:cafe-2":
                result.truth, result.reason_code = "unknown", "PARKING_QUERY_FAILED"
            return result

        try:
            await h.start()
            h.mode = "two"
            monkeypatch.setattr("app.modules.chat_v2.regional_preparation.assess", mixed)
            done = await h.say(
                "500m 등록 주차 카페",
                {"kind": "recommend", "purposes": [{"purpose": "cafe"}], "constraints": PARKING},
            )
            assert [c.place.value.place_id for c in done.response.recommendation.cards] == [
                "kakao:cafe-1"
            ]
            assert [a.truth for a in done.response.evaluation.assessments] == ["true", "unknown"]
            assert "CRITERIA_UNKNOWN" in done.response.warnings
            assert done.response.evaluation.assessments[0].facts[0]["radius_m"] == 500
            assert done.response.evaluation.assessments[0].facts[0]["total_count"] == 1
        finally:
            await h.close()

    asyncio.run(run())


def test_comparison_uses_confirmed_common_origin_without_changing_trip(normalized_database):
    async def run():
        h = Harness(normalized_database)
        try:
            await h.start()
            h.mode = "two"
            await h.say("카페 추천", {"kind": "recommend", "purposes": [{"purpose": "cafe"}]})
            before = await h.current()
            asked = await h.say(
                "둘 중 서울역에서 덜 걸리는 곳 주변 도로도",
                {
                    "kind": "compare",
                    "metric": "duration_seconds",
                    "origin_query": "서울역",
                    "include_traffic": True,
                    "targets": [
                        {"kind": "position", "position": 1},
                        {"kind": "position", "position": 2},
                    ],
                },
            )
            assert asked.response.clarification.required_field == "lookup_origin"
            done = await h.say(
                "첫 출발지", {"kind": "select_origin", "position": 1, "for_lookup": True}
            )
            assert done.response.comparison.outcome == "complete", done.model_dump()
            assert len(done.response.comparison.best_refs) == 2
            assert set(done.response.comparison.values.values()) == {600}
            assert any(item.topic == "traffic" for item in done.response.lookup.items)
            assert (await h.current()).conditions == before.conditions
            h.mode = "route_error"
            failed = await h.say(
                "둘 중 가까운 곳",
                {
                    "kind": "compare",
                    "metric": "distance_meters",
                    "targets": [
                        {"kind": "position", "position": 1},
                        {"kind": "position", "position": 2},
                    ],
                },
            )
            assert failed.response.clarification.required_field == "lookup_origin"
            await h.say(
                "서울역",
                {
                    "kind": "read",
                    "topics": ["driving"],
                    "origin_query": "서울역",
                    "targets": [
                        {"kind": "position", "position": 1},
                        {"kind": "position", "position": 2},
                    ],
                },
            )
            unavailable = await h.say(
                "첫 출발지",
                {
                    "kind": "select_origin",
                    "position": 1,
                    "for_lookup": True,
                },
            )
            assert unavailable.response.comparison.outcome == "incomplete"
            assert unavailable.response.comparison.best_refs == []
        finally:
            await h.close()

    asyncio.run(run())


def test_dependent_read_binds_new_result_and_never_uses_old_on_empty(normalized_database):
    async def run():
        h = Harness(normalized_database)
        try:
            await h.start()
            previous = await h.first_recommendation()
            h.place_id = "cafe-new"
            done = await h.say(
                "새로 추천하고 첫 주차",
                {"kind": "recommend", "after": {"position": 1, "topics": ["parking"]}},
            )
            assert done.response.continuation.status == "completed", done.model_dump()
            assert (
                done.response.recommendation.result_id != previous.response.recommendation.result_id
            )
            assert (
                str(done.response.recommendation.result_id)
                in done.response.continuation.lookup.items[0].place_ref
            )
            h.mode = "empty"
            calls = len(h.calls)
            empty = await h.say(
                "다시 추천 첫 주차",
                {"kind": "recommend", "after": {"position": 1, "topics": ["parking"]}},
            )
            assert empty.response.recommendation is None
            assert empty.response.continuation.status == "blocked"
            assert empty.response.continuation.reason_code == "DEPENDENCY_FAILED"
            assert "/v1/directions" not in h.calls[calls:]
        finally:
            await h.close()

    asyncio.run(run())


def test_dependent_driving_question_preserves_committed_destination(normalized_database):
    async def run():
        h = Harness(normalized_database)
        try:
            await h.start()
            first = await h.say(
                "카페 추천 첫 장소 이동시간도",
                {
                    "kind": "recommend",
                    "purposes": [{"purpose": "cafe"}],
                    "after": {"position": 1, "topics": ["driving"]},
                },
            )
            assert first.response.continuation.status == "waiting_input"
            assert first.response.recommendation
            before = await h.current()
            assert before.conditions.origin is None
            await h.say(
                "서울역",
                {
                    "kind": "read",
                    "origin_query": "서울역",
                    "topics": ["driving"],
                    "targets": [{"kind": "focus"}],
                },
            )
            done = await h.say(
                "첫 출발지",
                {
                    "kind": "select_origin",
                    "position": 1,
                    "for_lookup": True,
                },
            )
            assert done.response.lookup.items[0].status == "ok"
            assert (
                str(first.response.recommendation.result_id)
                in done.response.lookup.items[0].place_ref
            )
            assert (await h.current()).conditions == before.conditions
            async with h.writers() as db:
                assert await db.scalar(select(func.count()).select_from(ChatRunCommit)) == 1
        finally:
            await h.close()

    asyncio.run(run())


@pytest.mark.parametrize("stop", ["cancel", "recover"])
def test_stop_after_commit_preserves_recommendation_and_retries_only_read(
    normalized_database, stop
):
    async def run():
        h = Harness(normalized_database)
        rt = ChatRuntime(h.store, h.execution)
        entered, release = asyncio.Event(), asyncio.Event()
        original = h.execution.tools._read_one

        async def delayed(*args):
            entered.set()
            await release.wait()
            return await original(*args)

        try:
            await h.start()
            h.execution.tools._read_one = delayed
            h.model.interpret.return_value = DialogueProposal(
                decision={
                    "kind": "recommend",
                    "purposes": [{"purpose": "cafe"}],
                    "after": {"position": 1, "topics": ["parking"]},
                    "evidence_text": "카페",
                }
            )
            accepted = await rt.submit(
                h.credential, h.conversation.conversation_id, await request(h)
            )
            await rt.tick()
            await asyncio.wait_for(entered.wait(), 4)
            if stop == "cancel":
                done = await h.store.cancel(
                    h.credential, h.conversation.conversation_id, accepted.identity.run_id
                )
            else:
                async with h.writers.begin() as db:
                    await db.execute(
                        update(ChatRun)
                        .where(ChatRun.run_id == accepted.identity.run_id)
                        .values(lease_expires_at=h.store.clock() - timedelta(seconds=1))
                    )
                assert await h.store.recover() == 1
                done = await h.store.get_run(
                    h.credential, h.conversation.conversation_id, accepted.identity.run_id
                )
            assert (
                done.response.recommendation
                and done.response.snapshot.finalization.commit.state == "committed"
            )
            assert done.response.status == ("cancelled" if stop == "cancel" else "interrupted")
            release.set()
            await asyncio.gather(*rt.jobs)
            if stop == "recover":
                h.execution.tools._read_one = original
                retry = await run_request(
                    h,
                    rt,
                    await request(
                        h,
                        retry={
                            "type": "retry",
                            "source_run_id": accepted.identity.run_id,
                            "task_ids": ["lookup"],
                        },
                    ),
                )
                assert retry.response.kind == "answer" and retry.response.lookup.items[
                    0
                ].status in {"ok", "empty"}
            async with h.writers() as db:
                assert await db.scalar(select(func.count()).select_from(ChatRunCommit)) == 1
            assert h.model.interpret.await_count == 1
            assert (await h.current()).condition_version == 1
        finally:
            release.set()
            await rt.close()
            await h.close()

    asyncio.run(run())
