from unittest.mock import AsyncMock

import pytest

from app.api.routes.recommendations import get_recommendation_service
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
    ref = schema["paths"]["/api/v1/recommendations"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]["$ref"]
    assert ref.endswith("/ChatRequest")
    assert set(schema["components"]["schemas"]["ChatRequest"]["properties"]) == {
        "message",
        "conditions",
        "state",
    }
    assert set(schema["components"]["schemas"]["Conditions"]["properties"]) == {
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
