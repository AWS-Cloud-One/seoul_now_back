"""Fetch the complete integrated Seoul Real-Time City Data payload for one hotspot."""

import asyncio
from urllib.parse import quote

import httpx

from app.clients.seoul import CollectionError


async def collect_citydata(client: httpx.AsyncClient, key: str, area_name: str) -> dict:
    """Return the complete CITYDATA object; preserve every supplied section."""
    if not key:
        raise CollectionError("missing_citydata_api_key")
    if not area_name.strip():
        raise CollectionError("missing_area_name")
    url = (
        f"http://openapi.seoul.go.kr:8088/{quote(key, safe='')}"
        f"/json/citydata/1/5/{quote(area_name.strip(), safe='')}"
    )
    for attempt in range(3):
        try:
            response = await client.get(url, timeout=20, follow_redirects=False)
        except httpx.RequestError:
            if attempt < 2:
                await asyncio.sleep(0.5 * (attempt + 1))
                continue
            raise CollectionError("provider_network_error") from None
        if response.status_code >= 500 and attempt < 2:
            await asyncio.sleep(0.5 * (attempt + 1))
            continue
        if response.status_code != 200:
            raise CollectionError("provider_http_error")
        try:
            body = response.json()
            if not isinstance(body, dict):
                raise ValueError
            result = body.get("RESULT")
            code = result.get("CODE") if isinstance(result, dict) else None
            if code and code not in {"INFO-000", "INFO-200"}:
                raise CollectionError("provider_result_error")
            city = body.get("CITYDATA")
            if not isinstance(city, dict):
                raise ValueError
            if not str(city.get("AREA_NM") or "").strip():
                raise ValueError
            if not str(city.get("AREA_CD") or "").strip():
                raise ValueError
            return city
        except ValueError, KeyError, TypeError, AttributeError:
            raise CollectionError("provider_invalid_response") from None
    raise CollectionError("provider_unavailable")
