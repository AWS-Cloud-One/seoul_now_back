"""TOPIS link observations. Provider XML errors never expose credentials."""

from urllib.parse import quote
from xml.etree import ElementTree as ET

import httpx

from app.clients.seoul import CollectionError, seoul_result_error


async def collect_topis(client, key, link_id):
    if not key:
        raise CollectionError("missing_topis_api_key")
    if not link_id.isdigit() or len(link_id) != 10:
        raise CollectionError("invalid_link_id")
    try:
        response = await client.get(
            f"http://openapi.seoul.go.kr:8088/{quote(key, safe='')}"
            f"/xml/TrafficInfo/1/1000/{link_id}",
            timeout=20,
        )
        if response.status_code == 429:
            raise CollectionError("provider_quota_exceeded")
        if response.status_code in {401, 403}:
            raise CollectionError("provider_auth_error")
        if response.status_code >= 500:
            raise CollectionError("provider_server_error")
        if response.status_code != 200:
            raise CollectionError("provider_http_error")
        root = ET.fromstring(response.content)
        code = root.findtext(".//CODE")
        if code != "INFO-000":
            raise CollectionError(seoul_result_error(code))
        rows = [{node.tag: node.text for node in row} for row in root.findall("row")]
        if not rows or any(row.get("link_id") != link_id for row in rows):
            raise CollectionError("invalid_link_response")
        return {"rows": rows, "source_time": None}
    except httpx.RequestError:
        raise CollectionError("provider_network_error") from None
    except ET.ParseError:
        raise CollectionError("provider_invalid_xml") from None
