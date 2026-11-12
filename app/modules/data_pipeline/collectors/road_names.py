"""Official TOPIS LinkInfo names, matched to the requested service link ID."""

import re
from urllib.parse import quote
from xml.etree import ElementTree as ET

import httpx

from app.clients.seoul import CollectionError, seoul_result_error


async def collect_road_name(client, key, link_id):
    if not key:
        raise CollectionError("missing_topis_api_key")
    if not re.fullmatch(r"[0-9]{10}", link_id):
        raise CollectionError("invalid_link_id")
    try:
        response = await client.get(
            f"http://openapi.seoul.go.kr:8088/{quote(key, safe='')}/xml/LinkInfo/1/5/{link_id}",
            timeout=20,
            follow_redirects=False,
        )
    except httpx.RequestError:
        raise CollectionError("provider_network_error") from None
    if response.status_code == 429:
        raise CollectionError("provider_quota_exceeded")
    if response.status_code in {401, 403}:
        raise CollectionError("provider_auth_error")
    if response.status_code >= 500:
        raise CollectionError("provider_server_error")
    if response.status_code != 200:
        raise CollectionError("provider_http_error")
    try:
        root = ET.fromstring(response.content)
    except ET.ParseError:
        raise CollectionError("provider_invalid_xml") from None
    code = root.findtext(".//CODE")
    if code != "INFO-000":
        raise CollectionError(seoul_result_error(code))
    rows = root.findall("row")
    # A duplicate/conflicting or truncated response must never choose an arbitrary name.
    if root.findtext("list_total_count") != "1" or len(rows) != 1:
        raise CollectionError("invalid_road_name_response")
    row = rows[0]
    name = (row.findtext("road_name") or "").strip()
    if (row.findtext("link_id") or "").strip() != link_id or not name:
        raise CollectionError("invalid_road_name_response")
    return {"link_id": link_id, "name": name}
