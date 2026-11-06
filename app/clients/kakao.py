"""Shared Kakao I/O for legacy routes and recommendation orchestration."""

import math
from datetime import UTC, datetime

import httpx
from pydantic import ValidationError

from app.clients.errors import ProviderError
from app.schemas.recommendation import Conditions, Place

SEARCH_URL = "https://dapi.kakao.com/v2/local/search/keyword.json"
DIRECTIONS_URL = "https://apis-navi.kakaomobility.com/v1/directions"


async def request_json(client, settings, url, params):
    key = settings.kakao_rest_api_key.get_secret_value().strip()
    if not key:
        raise ProviderError(503, "kakao_not_configured", "APP_KAKAO_REST_API_KEY를 설정해 주세요.")
    try:
        response = await client.get(
            url, params=params, headers={"Authorization": f"KakaoAK {key}"}, timeout=10
        )
        if response.status_code in (401, 403):
            raise ProviderError(502, "kakao_auth", "카카오 키 또는 API 사용 권한을 확인해 주세요.")
        if response.status_code == 429:
            raise ProviderError(503, "kakao_quota", "카카오 API 사용 한도를 초과했습니다.")
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("invalid object")
        return data
    except httpx.TimeoutException:
        raise ProviderError(
            504, "kakao_timeout", "카카오 API 응답 시간이 초과되었습니다."
        ) from None
    except httpx.HTTPError, ValueError:
        raise ProviderError(502, "kakao_unavailable", "카카오 API 요청에 실패했습니다.") from None


def parse_route(data):
    routes = data.get("routes")
    if not isinstance(routes, list) or not routes or not isinstance(routes[0], dict):
        raise ProviderError(502, "route_invalid", "카카오 경로 응답을 확인할 수 없습니다.")
    route = routes[0]
    code = route.get("result_code")
    if type(code) is not int:
        raise ProviderError(502, "route_invalid", "카카오 경로 결과 코드가 유효하지 않습니다.")
    if code in {1, 101, 102, 103, 104, 105, 106, 107}:
        raise ProviderError(
            404, "route_not_found", "이 출발지와 목적지 사이의 자동차 경로가 없습니다."
        )
    if code != 0 or not isinstance(route.get("summary"), dict):
        raise ProviderError(502, "route_invalid", "카카오 경로 요약을 확인할 수 없습니다.")
    try:
        coordinates = []
        for section in route.get("sections", []):
            for road in section.get("roads", []):
                vertices = road.get("vertexes", [])
                coordinates.extend(
                    [vertices[i], vertices[i + 1]] for i in range(0, len(vertices) - 1, 2)
                )
        return {"summary": route["summary"], "coordinates": coordinates}
    except TypeError, AttributeError, IndexError:
        raise ProviderError(
            502, "route_invalid", "카카오 경로 좌표를 확인할 수 없습니다."
        ) from None


def parse_places(data):
    places = []
    if not isinstance(data.get("documents"), list):
        raise ProviderError(502, "kakao_invalid", "카카오 장소 응답 형식을 확인할 수 없습니다.")
    for doc in data["documents"]:
        try:
            places.append(
                Place(
                    id=doc["id"],
                    name=doc["place_name"],
                    x=doc["x"],
                    y=doc["y"],
                    address=doc.get("address_name") or "",
                    road_address=doc.get("road_address_name") or None,
                    phone=doc.get("phone") or None,
                    category_code=doc.get("category_group_code") or None,
                    category_name=doc.get("category_name") or None,
                    collected_at=datetime.now(UTC),
                    place_url=doc.get("place_url"),
                )
            )
        except KeyError, TypeError, ValidationError:
            continue
    return places


class KakaoClient:
    def __init__(self, client, settings):
        self.client, self.settings = client, settings

    async def search(self, conditions: Conditions, page: int):
        params = {
            "query": conditions.query,
            "x": conditions.x,
            "y": conditions.y,
            "sort": "distance",
            "page": page,
            "size": 15,
        }
        data = await request_json(self.client, self.settings, SEARCH_URL, params)
        meta = data.get("meta")
        end = meta.get("is_end") if isinstance(meta, dict) else None
        if not isinstance(end, bool):
            raise ProviderError(
                502, "kakao_invalid", "카카오 검색 페이지 정보를 확인할 수 없습니다."
            )
        return parse_places(data), end

    async def origins(self, name: str):
        data = await request_json(
            self.client, self.settings, SEARCH_URL, {"query": name, "page": 1, "size": 5}
        )
        return parse_places(data)

    async def duration(self, conditions: Conditions, place: Place):
        data = await request_json(
            self.client,
            self.settings,
            DIRECTIONS_URL,
            {
                "origin": f"{conditions.x},{conditions.y}",
                "destination": f"{place.x},{place.y}",
                "summary": "true",
                "priority": "RECOMMEND",
            },
        )
        try:
            seconds = parse_route(data)["summary"]["duration"]
            if type(seconds) is not int or seconds < 0:
                raise ValueError("invalid duration")
            return seconds
        except KeyError, TypeError, ValueError, AttributeError:
            raise ProviderError(
                502, "route_invalid", "예상 이동시간을 확인할 수 없습니다."
            ) from None

    async def region(self, x: float, y: float):
        data = await request_json(
            self.client,
            self.settings,
            "https://dapi.kakao.com/v2/local/geo/coord2regioncode.json",
            {"x": x, "y": y, "input_coord": "WGS84"},
        )
        documents = data.get("documents")
        if not isinstance(documents, list):
            raise ProviderError(502, "kakao_invalid", "지역 검증 응답을 확인할 수 없습니다.")
        regions = {d.get("region_1depth_name") for d in documents if isinstance(d, dict)}
        mapped = {"서울특별시": "seoul", "인천광역시": "incheon", "경기도": "gyeonggi"}
        known = {mapped[r] for r in regions if r in mapped}
        if len(known) == 1:
            return known.pop()
        return "other" if regions else None

    async def route(self, conditions, place):
        data = await request_json(
            self.client,
            self.settings,
            DIRECTIONS_URL,
            {
                "origin": f"{conditions.x},{conditions.y}",
                "destination": f"{place.x},{place.y}",
                "summary": "true",
                "priority": "RECOMMEND",
            },
        )
        summary = parse_route(data)["summary"]
        seconds, distance = summary.get("duration"), summary.get("distance")
        if (
            type(seconds) is not int
            or seconds < 0
            or type(distance) not in (int, float)
            or not math.isfinite(distance)
            or distance < 0
        ):
            raise ProviderError(502, "route_invalid", "경로의 시간·거리를 확인할 수 없습니다.")
        return seconds, distance
