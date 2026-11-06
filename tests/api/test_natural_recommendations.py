from unittest.mock import AsyncMock

import pytest

from app.api.recommendation_dependencies import (
    get_natural_recommendation_service as get_recommendation_service,
)
from app.modules.ai.explanation import ExplanationPlan
from app.modules.recommendation.state import StateCodec
from app.schemas.recommendation import Interpretation, Place, TrafficEvidence
from app.services.recommendation import RecommendationService


class Model:
    async def complete(self, prompt, payload, output):
        if output is ExplanationPlan:
            return ExplanationPlan(
                items=[
                    {"place_id": c["place_id"], "facts": list(c["facts"])}
                    for c in payload["ordered_candidates"]
                ]
            )
        message = payload["message"]
        if message == "다른 가게 추천해줘":
            return Interpretation(request_type="recommend_more", changes={})
        if message == "파스타로 바꿔줘":
            return Interpretation(request_type="update_conditions", changes={"query": "파스타"})
        return Interpretation(request_type="new_recommendation", changes={"query": "한식집"})


@pytest.fixture
def service(app):
    kakao = AsyncMock()
    kakao.region.return_value = "seoul"
    kakao.search.return_value = (
        [Place(id=str(i), name=str(i), x=127, y=37.5) for i in range(1, 7)],
        True,
    )
    kakao.duration.side_effect = lambda c, p: 600 + int(p.id)
    evidence = AsyncMock()
    evidence.for_place.return_value = (TrafficEvidence(), None)
    service = RecommendationService(Model(), kakao, evidence, StateCodec("test-only"))
    app.dependency_overrides[get_recommendation_service] = lambda: service
    return service


def test_natural_first_more_and_keyword_change(service, client):
    first = client.post(
        "/api/v1/recommendations",
        json={
            "message": "가족끼리 먹을 한식집 추천해줘",
            "conditions": {"x": 126.977, "y": 37.5657, "max_minutes": 30},
        },
    )
    assert first.status_code == 200, first.text
    one = first.json()
    assert one["conditions"]["query"] == "한식집"
    assert [c["place"]["id"] for c in one["recommendations"]] == ["1", "2", "3"]
    two = client.post(
        "/api/v1/recommendations", json={"message": "다른 가게 추천해줘", "state": one["state"]}
    ).json()
    assert [c["place"]["id"] for c in two["recommendations"]] == ["4", "5", "6"]
    assert service.kakao.search.await_count == 1
    changed = client.post(
        "/api/v1/recommendations", json={"message": "파스타로 바꿔줘", "state": two["state"]}
    ).json()
    assert changed["conditions"] == {**one["conditions"], "query": "파스타"}


def test_swagger_uses_natural_request(service, client):
    schema = client.get("/openapi.json").json()
    variants = schema["paths"]["/api/v1/recommendations"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]["anyOf"]
    assert {v["$ref"].split("/")[-1] for v in variants} == {"ChatRequest", "RecommendationRequest"}
    assert set(schema["components"]["schemas"]["ChatRequest"]["properties"]) == {
        "message",
        "conditions",
        "state",
    }
    conditions_ref = schema["components"]["schemas"]["ChatRequest"]["properties"]["conditions"][
        "anyOf"
    ][0]["$ref"].split("/")[-1]
    assert set(schema["components"]["schemas"][conditions_ref]["properties"]) == {
        "query",
        "x",
        "y",
        "max_minutes",
    }


def test_more_requires_valid_state(service, client):
    assert (
        client.post("/api/v1/recommendations", json={"message": "다른 가게 추천해줘"}).status_code
        == 422
    )
    assert (
        client.post(
            "/api/v1/recommendations", json={"message": "다른 가게 추천해줘", "state": "invalid"}
        ).status_code
        == 422
    )


def test_remaining_candidates_and_next_page_deduplicate(service, client):
    from app.schemas.recommendation import Place

    service.kakao.search.side_effect = [
        ([Place(id=str(i), name=str(i), x=127, y=37.5) for i in range(1, 6)], False),
        ([Place(id=str(i), name=str(i), x=127, y=37.5) for i in [1, 6, 7]], True),
    ]
    one = client.post(
        "/api/v1/recommendations", json={"message": "한식집", "conditions": {"x": 127, "y": 37.5}}
    ).json()
    two = client.post(
        "/api/v1/recommendations", json={"message": "다른 가게 추천해줘", "state": one["state"]}
    ).json()
    assert [c["place"]["id"] for c in two["recommendations"]] == ["4", "5", "6"]
    assert [call.args[1] for call in service.kakao.search.await_args_list] == [1, 2]


def test_api_ranking_unknown_traffic_uses_eta_despite_parking(service, client):
    from app.schemas.recommendation import ParkingEvidence

    service.kakao.duration.side_effect = lambda c, p: {"1": 659, "2": 600}.get(p.id, 1801)

    async def evidence(p):
        return TrafficEvidence(), ParkingEvidence(
            id=p.id, name="주차", distance_m=200 if p.id == "1" else 50
        )

    service.evidence.for_place.side_effect = evidence
    request = {"message": "한식집", "conditions": {"x": 127, "y": 37.5}}
    assert [
        c["place"]["id"]
        for c in client.post("/api/v1/recommendations", json=request).json()["recommendations"]
    ] == ["2", "1"]
    service.evidence.for_place.side_effect = None
    service.evidence.for_place.return_value = (TrafficEvidence(), None)
    assert [
        c["place"]["id"]
        for c in client.post("/api/v1/recommendations", json=request).json()["recommendations"]
    ] == ["2", "1"]


def test_origin_outside_service_area_is_rejected(service, client):
    service.kakao.region.return_value = "other"
    response = client.post(
        "/api/v1/recommendations", json={"message": "한식집", "conditions": {"x": 129, "y": 35}}
    )
    assert response.status_code == 422
    service.kakao.search.assert_not_awaited()


def test_http_adapters_through_natural_entrypoint_are_mocked(service, client):
    import json

    import httpx

    from app.clients.recommendation_kakao import KakaoClient
    from app.core.config import Settings
    from app.modules.ai.providers.openai import OpenAIProvider
    from app.modules.recommendation.settings import RecommendationSettings

    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.method == "POST":
            payload = json.loads(request.content)
            assert payload["store"] is False
            if payload["text"]["format"]["name"] == "Interpretation":
                output = {"request_type": "new_recommendation", "changes": {"query": "한식집"}}
            else:
                data = json.loads(payload["input"])
                output = {
                    "items": [
                        {"place_id": p["place_id"], "facts": list(p["facts"])}
                        for p in data["ordered_candidates"]
                    ]
                }
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "output": [{"content": [{"type": "output_text", "text": json.dumps(output)}]}],
                },
            )
        if "coord2regioncode" in request.url.path:
            return httpx.Response(
                200, json={"documents": [{"region_type": "B", "region_1depth_name": "서울특별시"}]}
            )
        if "keyword" in request.url.path:
            assert dict(request.url.params) == {
                "query": "한식집",
                "x": "126.977",
                "y": "37.5657",
                "sort": "distance",
                "page": "1",
                "size": "15",
            }
            return httpx.Response(
                200,
                json={
                    "meta": {"is_end": True},
                    "documents": [
                        {
                            "id": "123",
                            "place_name": "한식집",
                            "x": "127",
                            "y": "37.5",
                            "place_url": "https://place.map.kakao.com/123",
                        }
                    ],
                },
            )
        return httpx.Response(
            200, json={"routes": [{"result_code": 0, "summary": {"duration": 600}}]}
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service.kakao = KakaoClient(http, Settings(_env_file=None, kakao_rest_api_key="test-only"))
    service.model = OpenAIProvider(
        http, RecommendationSettings(_env_file=None, openai_api_key="test-only")
    )
    response = client.post(
        "/api/v1/recommendations",
        json={"message": "시청역 한식집", "conditions": {"x": 126.977, "y": 37.5657}},
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["recommendations"][0]["place"]["place_url"] == "https://place.map.kakao.com/123"
    assert result["explanation_source"] == "model_grounded"
    assert calls.count("/v1/responses") == 2
