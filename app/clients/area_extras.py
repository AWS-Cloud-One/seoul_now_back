"""On-demand selection of weather/commerce fields. No payload files or DB writes."""

from urllib.parse import quote

import httpx

from app.core.exceptions import ServiceError
from app.modules.data_pipeline.normalizers.citydata import CityDataError
from app.modules.data_pipeline.normalizers.citydata_sections import unwrap_citydata


class SeoulAreaExtras:
    def __init__(self, http, key):
        self.http, self.key = http, key

    async def fetch(self, area_code, area_name):
        return await self.fetch_sections(area_code, area_name, ("WEATHER_STTS", "LIVE_CMRCL_STTS"))

    async def fetch_sections(self, area_code, area_name, sections):
        if not self.key.strip():
            raise ServiceError("SERVICE_UNAVAILABLE", "도시데이터 조회 키가 설정되지 않았습니다.")
        url = (
            f"http://openapi.seoul.go.kr:8088/{quote(self.key, safe='')}"
            f"/json/citydata/1/5/{quote(area_name, safe='')}"
        )
        try:
            response = await self.http.get(url, timeout=20, follow_redirects=False)
            if response.status_code == 429:
                raise ServiceError("RATE_LIMITED", "도시데이터 조회 한도를 초과했습니다.")
            response.raise_for_status()
            city = unwrap_citydata(
                response.json(),
                expected_code=area_code,
                expected_name=area_name,
                require_envelope=True,
            )
            return {name: city.get(name) for name in sections}
        except CityDataError as exc:
            if str(exc) == "provider_quota_exceeded":
                raise ServiceError("RATE_LIMITED", "도시데이터 조회 한도를 초과했습니다.") from None
            raise ServiceError("PROVIDER_ERROR", "도시데이터 응답을 확인할 수 없습니다.") from None
        except httpx.TimeoutException:
            raise ServiceError("PROVIDER_TIMEOUT", "도시데이터 응답 시간이 초과됐습니다.") from None
        except httpx.HTTPError, ValueError, TypeError:
            raise ServiceError("PROVIDER_ERROR", "도시데이터 응답을 확인할 수 없습니다.") from None
