"""V2 preparation + existing services + real PostGIS; provider HTTP is synthetic."""

import asyncio

import httpx
import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import Settings
from app.db.session import read_session_factory, write_session_factory
from app.models import ChatConditionVersion, ChatSession, RecommendationResult
from app.modules.chat_v2.recommendation_factory import open_recommendation_preparer
from app.modules.chat_v2.results import PreparedRecommendation
from app.modules.chat_v2.validation import HeadcountPlanValidator
from app.modules.recommendation.progress import RecommendationWork, SearchProgress
from app.services.driving_requests import DrivingRequests
from tests.fixtures.chat_v2 import scenario
from tests.integration.test_recommendation_geography import seed
from tests.unit.recommendation.test_geography import sections
from tests.unit.services.test_part1_queries import route_body

pytestmark = pytest.mark.integration


def test_prepare_reads_postgis_and_preserves_trip_state_for_all_outcomes(normalized_database):
    async def run():
        engine = create_async_engine(normalized_database)
        sql, provider_calls = [], []
        mode = "ok"
        readers = read_session_factory(engine)

        def record_sql(conn, cursor, statement, parameters, context, executemany):
            sql.append(statement)

        async def counts():
            async with readers() as session:
                return [
                    await session.scalar(select(func.count()).select_from(model))
                    for model in (ChatSession, ChatConditionVersion, RecommendationResult)
                ]

        try:
            async with write_session_factory(engine).begin() as session:
                observed = await seed(session)
            before = await counts()
            event.listen(engine.sync_engine, "before_cursor_execute", record_sql)

            def respond(req):
                provider_calls.append(req.url.path)
                if req.url.path.endswith("coord2regioncode.json"):
                    return httpx.Response(
                        200,
                        json={
                            "documents": [{"region_type": "B", "region_1depth_name": "서울특별시"}]
                        },
                    )
                if req.url.path.endswith("keyword.json"):
                    if mode == "search_failure":
                        return httpx.Response(503, json={"private": "provider detail"})
                    return httpx.Response(
                        200,
                        json={
                            "documents": []
                            if mode == "empty"
                            else [
                                {
                                    "id": "v2-cafe",
                                    "place_name": "검증용 카페",
                                    "category_group_code": "FD6" if mode == "incomplete" else "CE7",
                                    "x": "126.98",
                                    "y": "37.57",
                                }
                            ],
                            "meta": {"is_end": mode != "incomplete"},
                        },
                    )
                if req.url.path == "/v1/directions":
                    if mode == "route_failure":
                        raise httpx.ReadTimeout("private", request=req)
                    return httpx.Response(200, json=route_body())
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
                                    at=observed.isoformat(),
                                ),
                            },
                        },
                    )
                raise AssertionError("Unexpected external request")

            settings = Settings(
                _env_file=None,
                database_url=normalized_database,
                kakao_rest_api_key="synthetic",
                seoul_citydata_api_key="synthetic",
            )
            async with readers() as session:
                assert await session.scalar(text("SHOW transaction_read_only")) == "on"
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
                for mode, expected in [
                    ("ok", "recommendation"),
                    ("empty", "no_candidates"),
                    ("incomplete", "insufficient_evidence"),
                    ("search_failure", "error"),
                    ("route_failure", "error"),
                ]:
                    request, proposal, fixture, _ = scenario()
                    plan = await HeadcountPlanValidator().validate(
                        request, fixture.plan.context, proposal
                    )
                    work = RecommendationWork(
                        version=plan.context.condition_version,
                        conditions=plan.context.current_conditions.model_copy(deep=True),
                        excluded=set(),
                        search=SearchProgress(),
                    )
                    async with open_recommendation_preparer(
                        readers,
                        http,
                        settings,
                        identity=plan.context.identity,
                        work=work,
                        driving_requests=DrivingRequests(budget=5, interval=0),
                    ) as preparer:
                        prepared = await preparer.prepare(plan)
                    assert prepared.outcome == expected
                    assert (
                        PreparedRecommendation.model_validate_json(prepared.model_dump_json())
                        == prepared
                    )
                    assert work.version == 7 and work.conditions.headcount == 2
                    assert work.excluded == set()
                    if mode == "ok":
                        card = prepared.cards[0]
                        assert card.place.value.place_id == "kakao:v2-cafe"
                        assert card.route.value.duration_seconds == 600
                        assert card.parking.status == "available" and card.parking.total_count == 1
                        assert prepared.search_scope.areas[0].geometry_version == "boundary-v1"
                        assert prepared.search_scope.areas[0].registered_parking_count == 2
                        assert card.ranking.traffic_applied
                    assert await counts() == before == [0, 0, 0]
            statements = "\n".join(sql).lower()
            assert "administrative_areas" in statements and "parking_facilities" in statements
            assert not any(
                s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for s in sql
            )
            assert any("citydata" in path for path in provider_calls)
        finally:
            await engine.dispose()

    asyncio.run(run())
