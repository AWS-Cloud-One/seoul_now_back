"""Seoul Open Data JSON transport. Never propagate URLs containing API keys."""

import asyncio
import re
from datetime import UTC, datetime
from urllib.parse import quote

import httpx


class CollectionError(Exception):
    """Safe, bounded error code for logs (never provider messages or URLs)."""


def seoul_result_error(code):
    """Classify application errors even when Seoul responds with HTTP 200."""
    if code in {"ERROR-500", "ERROR-600"}:
        return "provider_server_error"
    if code in {"INFO-100", "INFO-101"}:
        return "provider_auth_error"
    if code == "ERROR-337":
        return "provider_quota_exceeded"
    if code == "INFO-200":
        return "provider_no_observation"
    return "provider_result_error"


class SeoulClient:
    def __init__(self, client: httpx.AsyncClient, key: str, service: str = "GetParkInfo") -> None:
        self.client = client
        self.key = key
        self.service = service

    async def fetch_page(self, start: int, end: int) -> tuple[int, list[dict]]:
        document = await self.fetch_page_document(start, end)
        return document["total"], document["rows"]

    async def fetch_page_document(self, start: int, end: int, *, path_params=()):
        """Keep the decoded provider envelope and actual receipt for evidence-aware callers."""
        if not self.key:
            raise CollectionError("missing_api_key")
        base = f"http://openapi.seoul.go.kr:8088/{quote(self.key, safe='')}"
        url = f"{base}/json/{self.service}/{start}/{end}/"
        if path_params:
            url += "/".join(quote(str(value), safe="") for value in path_params)
        for attempt in range(3):
            try:
                response = await self.client.get(url, timeout=20, follow_redirects=False)
            except httpx.RequestError:
                if attempt < 2:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                raise CollectionError("provider_network_error") from None
            received_at = datetime.now(UTC)
            if response.status_code >= 500 and attempt < 2:
                await asyncio.sleep(0.5 * (attempt + 1))
                continue
            if response.status_code != 200:
                if response.status_code == 429:
                    raise CollectionError("provider_quota_exceeded")
                if response.status_code in {401, 403}:
                    raise CollectionError("provider_auth_error")
                if response.status_code >= 500:
                    raise CollectionError("provider_server_error")
                raise CollectionError("provider_http_error")
            try:
                body = response.json()
                data = body.get(self.service, body)
                result = data.get("RESULT", {})
                code = result.get("CODE")
                if code == "INFO-200":
                    return {"total": 0, "rows": [], "body": body, "received_at": received_at}
                if code != "INFO-000":
                    error = seoul_result_error(code)
                    if error == "provider_server_error" and attempt < 2:
                        await asyncio.sleep(0.5 * (attempt + 1))
                        continue
                    raise CollectionError(error)
                raw_count = data["list_total_count"]
                if (
                    isinstance(raw_count, bool)
                    or not isinstance(raw_count, (str, int))
                    or not re.fullmatch(r"[0-9]+", str(raw_count).strip())
                ):
                    raise ValueError
                count = int(raw_count)
                rows = data["row"]
                if (
                    count < 0
                    or not isinstance(rows, list)
                    or not all(isinstance(row, dict) for row in rows)
                ):
                    raise ValueError
                return {"total": count, "rows": rows, "body": body, "received_at": received_at}
            except ValueError, KeyError, TypeError, AttributeError:
                raise CollectionError("provider_invalid_response") from None
        raise CollectionError("provider_unavailable")
