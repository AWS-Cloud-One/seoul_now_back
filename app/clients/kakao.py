"""Shared Kakao HTTP transport; safe errors, no retries or credential logging."""

import httpx

DRIVING_OPTIONS = {"priority": "RECOMMEND", "alternatives": "false", "road_details": "false"}


class KakaoError(Exception):
    def __init__(self, status: int, message: str, retry_after_seconds: int | None = None):
        super().__init__(message)
        self.status, self.message = status, message
        self.retry_after_seconds = retry_after_seconds


class KakaoConfigurationError(KakaoError):
    """Credentials or API access must be fixed before retrying."""


async def kakao_get(http: httpx.AsyncClient, url: str, params: dict, key: str) -> dict:
    key = key.strip()
    if not key:
        raise KakaoConfigurationError(503, "백엔드 APP_KAKAO_REST_API_KEY를 설정해 주세요.")
    try:
        response = await http.get(url, params=params, headers={"Authorization": f"KakaoAK {key}"})
        if response.status_code == 429:
            raw = response.headers.get("Retry-After", "")
            wait = int(raw) if raw.isascii() and raw.isdecimal() and len(raw) <= 9 else None
            raise KakaoError(429, "카카오 API 사용 한도를 초과했습니다.", wait)
        if response.status_code in (401, 403):
            raise KakaoConfigurationError(502, "카카오 키 또는 해당 API 사용 권한을 확인해 주세요.")
        if response.status_code == 400:
            body = response.json()
            if isinstance(body, dict) and body.get("code") == -10:
                # Documented quota exhaustion. The reset time is not supplied: wait conservatively.
                raise KakaoError(429, "카카오 API 사용 한도를 초과했습니다.", 86400)
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict):
            raise ValueError
        return body
    except httpx.TimeoutException:
        raise KakaoError(504, "카카오 API 응답 시간이 초과되었습니다.") from None
    except httpx.HTTPError, ValueError:
        raise KakaoError(502, "카카오 API 요청에 실패했습니다.") from None


class KakaoLocalClient:
    def __init__(self, http: httpx.AsyncClient, key: str):
        self.http, self.key = http, key

    async def keyword(self, query: str, *, page: int, size: int) -> dict:
        return await kakao_get(
            self.http,
            "https://dapi.kakao.com/v2/local/search/keyword.json",
            {"query": query, "page": page, "size": size},
            self.key,
        )

    async def address(self, query: str, *, size: int, page: int = 1) -> dict:
        return await kakao_get(
            self.http,
            "https://dapi.kakao.com/v2/local/search/address.json",
            {"query": query, "page": page, "size": size},
            self.key,
        )

    async def nearby_keyword(self, query: str, coordinates, *, page: int) -> dict:
        return await kakao_get(
            self.http,
            "https://dapi.kakao.com/v2/local/search/keyword.json",
            {
                "query": query,
                "x": coordinates.longitude,
                "y": coordinates.latitude,
                "page": page,
                "size": 15,
                "sort": "distance",
            },
            self.key,
        )

    async def directions(self, origin, destination, *, include_geometry=False) -> dict:
        return await kakao_get(
            self.http,
            "https://apis-navi.kakaomobility.com/v1/directions",
            {
                "origin": f"{origin.longitude},{origin.latitude}",
                "destination": f"{destination.longitude},{destination.latitude}",
                "summary": "false" if include_geometry else "true",
                **DRIVING_OPTIONS,
            },
            self.key,
        )

    async def region(self, latitude: float, longitude: float) -> dict:
        return await kakao_get(
            self.http,
            "https://dapi.kakao.com/v2/local/geo/coord2regioncode.json",
            {"x": longitude, "y": latitude, "input_coord": "WGS84"},
            self.key,
        )

    async def coordinate_address(self, latitude: float, longitude: float) -> dict:
        return await kakao_get(
            self.http,
            "https://dapi.kakao.com/v2/local/geo/coord2address.json",
            {"x": longitude, "y": latitude, "input_coord": "WGS84"},
            self.key,
        )
