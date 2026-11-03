"""Bounded supplemental HTTP and file reads; no database access or scheduling."""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote

from app.clients.seoul import CollectionError, SeoulClient
from app.modules.data_pipeline.collectors.official_files import read_xlsx_rows


def read_sensor_rows(path):
    return read_xlsx_rows(path)[1:]


async def sgis_json(http, token, path, params):
    response = await http.get(
        "https://sgisapi.kostat.go.kr/OpenAPI3/" + path,
        params={"accessToken": token, **params},
    )
    response.raise_for_status()
    data = response.json()
    if data.get("errCd") != 0:
        raise CollectionError("sgis_provider_error")
    return data


async def collect_areas(http, settings):
    response = await http.get(
        "https://sgisapi.kostat.go.kr/OpenAPI3/auth/authentication.json",
        params={
            "consumer_key": settings.sgis_consumer_key.get_secret_value(),
            "consumer_secret": settings.sgis_consumer_secret.get_secret_value(),
        },
    )
    response.raise_for_status()
    token = (response.json().get("result") or {}).get("accessToken")
    if not token:
        raise CollectionError("sgis_auth_failed")
    districts = (await sgis_json(http, token, "addr/stage.json", {"cd": "11", "pg_yn": "0"}))[
        "result"
    ]
    areas = []
    for district in districts:
        rows = (
            await sgis_json(http, token, "addr/stage.json", {"cd": district["cd"], "pg_yn": "1"})
        )["result"]
        for row in rows:
            if not row.get("pg"):
                raise CollectionError("sgis_missing_boundary")
            areas.append((district, row))
    boundary = await sgis_json(
        http,
        token,
        "boundary/hadmarea.geojson",
        {"adm_cd": "11", "year": "2025", "low_search": "0"},
    )
    return {"areas": areas, "boundary": boundary}


async def collect_sdot(http, settings):
    client = SeoulClient(http, settings.seoul_sdot_api_key.get_secret_value(), "sDoTPeople")
    rows = []
    total = 0
    # Recent rolling window only; do not expand the existing 10,000-row scope.
    for start in range(1, 10001, 1000):
        total, page = await client.fetch_page(start, start + 999)
        rows.extend(page)
        if start + 999 >= total:
            break
    return {"rows": rows, "provider_total": total}


def read_cctv_probe(path):
    path = Path(path)
    return (
        json.loads(path.read_text(encoding="utf-8")),
        datetime.fromtimestamp(path.stat().st_mtime, UTC),
    )


async def collect_cctv(http, settings, *, probe_path=None):
    if probe_path is not None:
        payload, fetched = await asyncio.to_thread(read_cctv_probe, probe_path)
    else:
        # One request per run; do not retry against the provider's limited quota.
        response = await http.get(
            "https://openapi.its.go.kr:9443/cctvInfo",
            params={
                "apiKey": settings.its_api_key.get_secret_value(),
                "type": "all",
                "cctvType": 1,
                "minX": 126.7,
                "maxX": 127.25,
                "minY": 37.4,
                "maxY": 37.75,
                "getType": "json",
            },
        )
        response.raise_for_status()
        payload = response.json()
        fetched = datetime.now(UTC)
    rows = payload.get("response", {}).get("data")
    if not isinstance(rows, list):
        raise CollectionError("its_invalid_response")
    return {"rows": rows, "fetched_at": fetched}


async def collect_national_parking(http, settings):
    result = {}
    for method in ("PrkSttusInfo", "PrkOprInfo", "PrkRealtimeInfo"):
        rows = []
        total = 0
        for page in range(1, 6):
            response = await http.get(
                f"https://apis.data.go.kr/B553881/Parking_v1/{method}_v1",
                params={
                    "serviceKey": unquote(settings.data_go_kr_api_key.get_secret_value()),
                    "numOfRows": 1000,
                    "pageNo": page,
                    "format": 2,
                    "sidoCd": "11",
                },
            )
            response.raise_for_status()
            data = response.json()
            if str(data.get("resultCode")) not in {"0", "00"}:
                raise CollectionError("parking_provider_error")
            batch = data.get(method) or []
            if not isinstance(batch, list):
                raise CollectionError("parking_invalid_response")
            total = int(data["totalCount"])
            rows.extend(batch)
            if len(batch) < 1000 or len(rows) >= total:
                break
        result[method] = {"rows": rows, "provider_total": total, "complete": len(rows) >= total}
    return {
        "source": "한국교통안전공단",
        "datasets": result,
        "scope": "서울 필터·기능별 최대 5000행",
        "notice": (
            "부분 수집이며 주차장 관리 ID로만 연결합니다. "
            "측정 시각이 없는 빈자리는 현재 빈자리로 보증하지 않습니다."
        ),
    }
