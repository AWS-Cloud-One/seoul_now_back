"""Fetch the complete integrated Seoul Real-Time City Data payload for one hotspot."""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import quote

import httpx

from app.clients.seoul import CollectionError
from app.modules.data_pipeline.normalizers.citydata import CityDataError
from app.modules.data_pipeline.normalizers.citydata_sections import unwrap_citydata
from app.modules.data_pipeline.normalizers.road_geometry import citydata_line


@dataclass(frozen=True)
class CityDataDocument:
    body: dict
    received_at: datetime | None
    require_envelope: bool = True


class CityDataResponseError(CollectionError):
    def __init__(self, code, document):
        super().__init__(code)
        self.document = document


async def collect_citydata(
    client: httpx.AsyncClient, key: str, area_name: str, *, expected_code: str
) -> CityDataDocument:
    """Keep the full decoded envelope and receipt, including rejected provider documents."""
    if not key:
        raise CollectionError("missing_citydata_api_key")
    if not area_name.strip():
        raise CollectionError("missing_area_name")
    if not expected_code.strip():
        raise CollectionError("missing_area_code")
    area_name, expected_code = area_name.strip(), expected_code.strip()
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
        except ValueError:
            raise CollectionError("provider_invalid_response") from None
        document = CityDataDocument(body, received_at)
        try:
            if not isinstance(body, dict) or "RESULT" not in body:
                raise CityDataError("provider_invalid_response")
            unwrap_citydata(body, expected_code=expected_code, expected_name=area_name)
        except CityDataError as exc:
            if str(exc) == "provider_server_error" and attempt < 2:
                await asyncio.sleep(0.5 * (attempt + 1))
                continue
            raise CityDataResponseError(str(exc), document) from None
        return document
    raise CollectionError("provider_unavailable")


def resolve_citydata_area(value, targets):
    """Resolve the CLI's official name or code before making any provider request."""
    value = value.strip()
    matches = [(code, name) for code, name in targets.items() if value in {code, name}]
    if len(matches) != 1:
        raise CollectionError("unknown_or_ambiguous_area")
    return matches[0]


async def collect_road_reference_inputs(client, key, targets):
    """Keep only road IDs/coordinates in memory during an explicit reference import."""
    if not targets:
        raise CollectionError("import_place_reference_first")
    inputs = []
    for code, name in targets.items():
        document = await collect_citydata(client, key, name, expected_code=code)
        city = unwrap_citydata(document.body, expected_code=code, expected_name=name)
        traffic = city.get("ROAD_TRAFFIC_STTS")
        rows = traffic.get("ROAD_TRAFFIC_STTS") if isinstance(traffic, dict) else None
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise CollectionError("invalid_road_reference_response")
        if rows and not any(
            isinstance(row.get("LINK_ID"), str)
            and row["LINK_ID"].strip()
            and citydata_line(row)[1] is None
            for row in rows
        ):
            raise CollectionError("invalid_road_reference_response")
        inputs.append(
            {
                "area_code": code,
                "received_at": document.received_at.isoformat() if document.received_at else None,
                "data": {
                    "ROAD_TRAFFIC_STTS": {
                        "ROAD_TRAFFIC_STTS": [
                            {key: row.get(key) for key in ("LINK_ID", "XYLIST")} for row in rows
                        ]
                    }
                },
            }
        )
    return inputs
