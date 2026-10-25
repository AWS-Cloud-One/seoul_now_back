"""Server-side Kakao requests. Provider credentials never enter responses."""

from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.dependencies import get_settings
from app.core.config import Settings

router = APIRouter(prefix="/kakao", tags=["kakao"])
Config = Annotated[Settings, Depends(get_settings)]


async def request_provider(url: str, params: dict, settings: Settings) -> dict:
    key = settings.kakao_rest_api_key.get_secret_value().strip()
    if not key:
        raise HTTPException(503, "백엔드 APP_KAKAO_REST_API_KEY를 설정해 주세요.")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(
                url, params=params, headers={"Authorization": f"KakaoAK {key}"}
            )
        if response.status_code in (401, 403):
            raise HTTPException(502, "카카오 키 또는 해당 API 사용 권한을 확인해 주세요.")
        if response.status_code == 429:
            raise HTTPException(503, "카카오 API 사용 한도를 초과했습니다.")
        response.raise_for_status()
        return response.json()
    except httpx.TimeoutException:
        raise HTTPException(504, "카카오 API 응답 시간이 초과되었습니다.") from None
    except httpx.HTTPError, ValueError:
        raise HTTPException(502, "카카오 API 요청에 실패했습니다.") from None


@router.get("/places")
async def places(
    settings: Config,
    query: Annotated[str, Query(min_length=1, max_length=100)],
    lat: Annotated[float, Query(ge=33, le=39)] = 37.5665,
    lon: Annotated[float, Query(ge=124, le=132)] = 126.978,
    page: Annotated[int, Query(ge=1, le=45)] = 1,
):
    return await request_provider(
        "https://dapi.kakao.com/v2/local/search/keyword.json",
        {"query": query, "x": lon, "y": lat, "page": page, "size": 15},
        settings,
    )


@router.get("/directions")
async def directions(
    settings: Config,
    origin_lat: Annotated[float, Query(ge=33, le=39)],
    origin_lon: Annotated[float, Query(ge=124, le=132)],
    destination_lat: Annotated[float, Query(ge=33, le=39)],
    destination_lon: Annotated[float, Query(ge=124, le=132)],
):
    data = await request_provider(
        "https://apis-navi.kakaomobility.com/v1/directions",
        {
            "origin": f"{origin_lon},{origin_lat}",
            "destination": f"{destination_lon},{destination_lat}",
            "summary": "false",
            "priority": "RECOMMEND",
        },
        settings,
    )
    route = next(iter(data.get("routes") or []), None)
    if not route or route.get("result_code") != 0:
        raise HTTPException(404, "이 출발지와 목적지 사이의 자동차 경로를 찾지 못했습니다.")
    coordinates = []
    for section in route.get("sections", []):
        for road in section.get("roads", []):
            vertices = road.get("vertexes", [])
            coordinates.extend(
                [vertices[i], vertices[i + 1]] for i in range(0, len(vertices) - 1, 2)
            )
    return {"summary": route["summary"], "coordinates": coordinates}
