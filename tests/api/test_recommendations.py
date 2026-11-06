from unittest.mock import AsyncMock

import pytest

from app.api.routes.recommendations import get_recommendation_service
from app.clients.errors import ProviderError
from app.modules.ai.explanation import ExplanationPlan
from app.modules.recommendation.state import StateCodec
from app.schemas.recommendation import Interpretation, Place, TrafficEvidence
from app.services.recommendation import RecommendationService


class Model:
    async def complete(self, instructions, payload, output_type):
        if output_type is ExplanationPlan:
            return ExplanationPlan(
                items=[
                    {"place_id": c["place_id"], "facts": list(c["facts"])}
                    for c in payload["ordered_candidates"]
                ]
            )
        message = payload["message"]
        if message == "다른 곳 추천해줘":
            return Interpretation(request_type="recommend_more", changes={})
        if message == "고기 말고 파스타":
            return Interpretation(request_type="update_conditions", changes={"query": "파스타"})
        return Interpretation(request_type="new_recommendation", changes={"query": "고기"})


class Kakao:
    def __init__(self):
        self.searches = []
        self.routes = []

    async def search(self, conditions, page):
        self.searches.append((conditions.query, page))
        ids = ["1", "2", "3", "4", "5"] if page == 1 else ["1", "6", "7"]
        return [Place(id=i, name=conditions.query + i, x=127, y=37.5) for i in ids], page == 2

    async def duration(self, conditions, place):
        self.routes.append(place.id)
        return 600 + int(place.id) * 10


class Evidence:
    async def load(self):
        pass

    async def for_place(self, place):
        return TrafficEvidence(), None


@pytest.fixture
def dependencies(app):
    kakao = Kakao()
    service = RecommendationService(Model(), kakao, Evidence(), StateCodec("test-secret"))
    app.dependency_overrides[get_recommendation_service] = lambda: service
    return service, kakao


def first(client, **overrides):
    return client.post(
        "/api/v1/recommendations",
        json={
            "message": "고기 추천",
            "conditions": {"x": 126.978, "y": 37.5665},
            **overrides,
        },
    )


def test_new_more_and_keyword_change_preserve_conditions(dependencies, client):
    _, kakao = dependencies
    initial = first(client)
    assert initial.status_code == 200, initial.text
    one = initial.json()
    assert [c["place"]["id"] for c in one["recommendations"]] == ["1", "2", "3"]
    assert one["conditions"]["max_minutes"] == 30
    two = client.post(
        "/api/v1/recommendations", json={"message": "다른 곳 추천해줘", "state": one["state"]}
    ).json()
    assert [c["place"]["id"] for c in two["recommendations"]] == ["4", "5", "6"]
    assert kakao.searches == [("고기", 1), ("고기", 2)]
    assert kakao.routes.count("4") == 2  # Unexposed page-one candidate revalidated.
    three = client.post(
        "/api/v1/recommendations", json={"message": "다른 곳 추천해줘", "state": two["state"]}
    ).json()
    assert [c["place"]["id"] for c in three["recommendations"]] == ["7"]
    assert three["status"] == "exhausted"
    four = client.post(
        "/api/v1/recommendations", json={"message": "다른 곳 추천해줘", "state": three["state"]}
    ).json()
    assert four["recommendations"] == [] and four["status"] == "exhausted"
    changed = client.post(
        "/api/v1/recommendations", json={"message": "고기 말고 파스타", "state": two["state"]}
    ).json()
    assert changed["conditions"] == {**one["conditions"], "query": "파스타"}
    assert [c["place"]["id"] for c in changed["recommendations"]] == ["1", "2", "3"]
    assert kakao.searches[-1] == ("파스타", 1)


def test_remaining_three_used_without_new_page(dependencies, client):
    service, kakao = dependencies
    original = kakao.search

    async def search(conditions, page):
        places, end = await original(conditions, page)
        return places + [Place(id="8", name="8", x=127, y=37.5)], end

    kakao.search = search
    one = first(client).json()
    two = client.post(
        "/api/v1/recommendations", json={"message": "다른 곳 추천해줘", "state": one["state"]}
    ).json()
    assert [c["place"]["id"] for c in two["recommendations"]] == ["4", "5", "8"]
    assert kakao.searches == [("고기", 1)]


def test_request_without_transport_uses_driving_routes(dependencies, client):
    _, kakao = dependencies
    response = first(client, conditions={"x": 127, "y": 37.5})
    assert response.status_code == 200
    assert len(response.json()["recommendations"]) == 3
    assert "transport" not in response.json()["conditions"]
    assert kakao.routes


def test_bad_state_and_more_without_state_are_rejected(dependencies, client):
    assert first(client, state="tampered").status_code == 422
    assert (
        client.post("/api/v1/recommendations", json={"message": "다른 곳 추천해줘"}).status_code
        == 422
    )


def test_route_failure_excludes_candidate_and_outage_is_not_exhaustion(dependencies, client):
    _, kakao = dependencies
    original = kakao.duration

    async def duration(conditions, place):
        if place.id == "1":
            raise ProviderError(504, "kakao_timeout", "timeout")
        if place.id == "2":
            return 1801
        return await original(conditions, place)

    kakao.duration = duration
    result = first(client).json()
    assert [c["place"]["id"] for c in result["recommendations"]] == ["3", "4", "5"]
    kakao.duration = AsyncMock(side_effect=ProviderError(503, "kakao_quota", "quota"))
    assert first(client).status_code == 503


def test_unconfigured_openai_is_explicit_503(client):
    response = first(client)
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "openai_not_configured"


def test_api_reorders_close_car_candidates_by_traffic(dependencies, client):
    service, kakao = dependencies
    kakao.duration = AsyncMock(side_effect=lambda c, p: {"1": 1200, "2": 1320}.get(p.id, 2000))

    async def evidence(place):
        return TrafficEvidence(status="정체" if place.id == "1" else "원활"), None

    service.evidence.for_place = evidence
    result = first(client).json()
    assert [c["place"]["id"] for c in result["recommendations"]] == ["2", "1"]
    assert "도착지 주변 도로" in result["recommendations"][0]["explanation"]
    service.evidence.for_place = AsyncMock(return_value=(TrafficEvidence(), None))
    result = first(client).json()
    assert [c["place"]["id"] for c in result["recommendations"]] == ["1", "2"]


def test_page_budget_reports_incomplete_and_continues_at_next_page(dependencies, client):
    _, kakao = dependencies

    async def search(conditions, page):
        kakao.searches.append((conditions.query, page))
        return [Place(id=str(page), name="후보", x=127, y=37.5)], False

    kakao.search = search
    kakao.duration = AsyncMock(return_value=1801)
    one = first(client).json()
    assert one["status"] == "search_incomplete" and one["more_available"]
    assert one["recommendations"] == []
    client.post(
        "/api/v1/recommendations", json={"message": "다른 곳 추천해줘", "state": one["state"]}
    )
    assert kakao.searches == [("고기", n) for n in (1, 2, 3, 4, 5, 6)]


def test_origin_resolution_uses_verified_coordinates_or_asks_for_selection(dependencies, client):
    _, kakao = dependencies
    kakao.origins = AsyncMock(
        return_value=[Place(id="station", name="시청역", x=126.978, y=37.5665)]
    )
    response = first(client, conditions={"origin_name": "시청역"}).json()
    assert response["conditions"]["x"] == 126.978
    kakao.origins = AsyncMock(
        return_value=[Place(id="station", name="시청역 1호선", x=126.978, y=37.5665)]
    )
    response = first(client, conditions={"origin_name": "시청역"}).json()
    assert response["status"] == "needs_clarification"
    assert response["origin_options"][0]["id"] == "station"
    selected = client.post(
        "/api/v1/recommendations",
        json={
            "message": "다른 곳 추천해줘",
            "state": response["state"],
            "conditions": {"x": 126.978, "y": 37.5665},
        },
    ).json()
    assert selected["conditions"]["query"] == "고기"
    assert len(selected["recommendations"]) == 3


def test_real_provider_adapters_and_stored_road_projection_through_chat(app, client):
    import json
    from datetime import UTC, datetime
    from types import SimpleNamespace
    from unittest.mock import patch

    import httpx
    from pydantic import SecretStr

    app.state.settings.openai_api_key = SecretStr("test-openai")
    app.state.settings.kakao_rest_api_key = SecretStr("test-kakao")
    source_time = datetime.now(UTC).isoformat()
    snapshot = SimpleNamespace(
        area_code="POI001",
        collected_at=datetime.now(UTC),
        data={
            "ROAD_TRAFFIC_STTS": {
                "AVG_ROAD_DATA": {"ROAD_TRAFFIC_TIME": source_time},
                "ROAD_TRAFFIC_STTS": [
                    {"LINK_ID": "r1", "XYLIST": "127_37.5|127.001_37.5", "IDX": "정체"},
                    {"LINK_ID": "r2", "XYLIST": "127.02_37.5|127.021_37.5", "IDX": "원활"},
                ],
            }
        },
    )

    async def post(self, url, **kwargs):
        body = kwargs["json"]
        if body["text"]["format"]["name"] == "Interpretation":
            output = {"request_type": "new_recommendation", "changes": {"query": "파스타"}}
        else:
            payload = json.loads(body["input"])
            output = {
                "items": [
                    {"place_id": c["place_id"], "facts": ["eta", "traffic"]}
                    for c in payload["ordered_candidates"]
                ]
            }
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": json.dumps(output)}],
                    }
                ],
            },
        )

    async def get(self, url, **kwargs):
        params = kwargs["params"]
        if "keyword" in url:
            assert params["sort"] == "distance" and params["size"] == 15
            payload = {
                "meta": {"is_end": True},
                "documents": [
                    {"id": "1", "place_name": "A", "x": "127", "y": "37.5"},
                    {"id": "2", "place_name": "B", "x": "127.02", "y": "37.5"},
                ],
            }
        else:
            duration = 1200 if params["destination"] == "127.0,37.5" else 1320
            payload = {"routes": [{"result_code": 0, "summary": {"duration": duration}}]}
        return httpx.Response(200, request=httpx.Request("GET", url), json=payload)

    with (
        patch("httpx.AsyncClient.post", new=post),
        patch("httpx.AsyncClient.get", new=get),
        patch(
            "app.repositories.recommendation.CityDataRepository.list_latest",
            new=AsyncMock(return_value=[snapshot]),
        ),
        patch(
            "app.repositories.recommendation.ParkingRepository.nearby",
            new=AsyncMock(return_value={"items": []}),
        ),
    ):
        response = first(client)
    assert response.status_code == 200, response.text
    result = response.json()
    assert [c["place"]["id"] for c in result["recommendations"]] == ["2", "1"]
    assert result["explanation_source"] == "model_grounded"
    assert result["recommendations"][0]["traffic"]["road_ids"] == ["r2"]
