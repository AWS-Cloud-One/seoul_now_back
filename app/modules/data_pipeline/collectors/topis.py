"""TOPIS link observations. Provider XML errors never expose credentials."""

from urllib.parse import quote
from xml.etree import ElementTree as ET

import httpx

from app.clients.seoul import CollectionError


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
        if response.status_code != 200:
            raise CollectionError("provider_http_error")
        root = ET.fromstring(response.content)
        code = root.findtext(".//CODE")
        if code == "ERROR-337":
            raise CollectionError("provider_quota_exceeded")
        if code in {"INFO-100", "INFO-101"}:
            raise CollectionError("provider_auth_error")
        if root.findtext(".//CODE") == "INFO-200":
            raise CollectionError("provider_no_observation")
        if root.findtext(".//CODE") != "INFO-000":
            raise CollectionError("provider_result_error")
        rows = [{node.tag: node.text for node in row} for row in root.findall("row")]
        if not rows or any(row.get("link_id") != link_id for row in rows):
            raise CollectionError("invalid_link_response")
        return {"rows": rows, "source_time": None}
    except httpx.RequestError, ET.ParseError:
        raise CollectionError("provider_network_or_xml_error") from None
