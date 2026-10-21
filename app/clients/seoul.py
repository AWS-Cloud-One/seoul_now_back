"""Seoul Open Data JSON transport. Never propagate URLs containing API keys."""

import asyncio
from urllib.parse import quote

import httpx


class CollectionError(Exception):
    """Safe, bounded error code for logs (never provider messages or URLs)."""


class SeoulClient:
    def __init__(self, client: httpx.AsyncClient, key: str, service: str = "GetParkInfo") -> None:
        self.client = client
        self.key = key
        self.service = service

    async def parking_page(self, start: int, end: int) -> tuple[int, list[dict]]:
        if not self.key:
            raise CollectionError("missing_api_key")
        base = f"http://openapi.seoul.go.kr:8088/{quote(self.key, safe='')}"
        url = f"{base}/json/{self.service}/{start}/{end}/"
        for attempt in range(3):
            try:
                response = await self.client.get(url, timeout=20, follow_redirects=False)
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
                data = body.get(self.service, body)
                result = data.get("RESULT", {})
                code = result.get("CODE")
                if code == "INFO-200":
                    return 0, []
                if code != "INFO-000":
                    raise CollectionError("provider_result_error")
                count = int(data["list_total_count"])
                rows = data["row"]
                if (
                    count < 0
                    or not isinstance(rows, list)
                    or not all(isinstance(row, dict) for row in rows)
                ):
                    raise ValueError
                return count, rows
            except ValueError, KeyError, TypeError, AttributeError:
                raise CollectionError("provider_invalid_response") from None
        raise CollectionError("provider_unavailable")
