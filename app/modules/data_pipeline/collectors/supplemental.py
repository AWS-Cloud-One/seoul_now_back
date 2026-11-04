"""Bounded supplemental HTTP and file reads; no database access or scheduling."""

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo

from app.clients.seoul import CollectionError, SeoulClient
from app.modules.data_pipeline.collectors.national_parking import (
    collect_national_parking as collect_national_parking,
)
from app.modules.data_pipeline.collectors.official_files import read_xlsx_rows
from app.modules.data_pipeline.identity import fingerprint

SGIS_API_BASE = "https://sgisapi.mods.go.kr/OpenAPI3/"


def read_sensor_file(path):
    path = Path(path)
    body = path.read_bytes()
    rows = read_xlsx_rows(BytesIO(body))
    digest = hashlib.sha256(body).hexdigest()
    return {
        "body": body,
        "filename": path.name,
        "sha256": digest,
        "byte_size": len(body),
        "header": rows[0] if rows else {},
        "rows": rows[1:],
        "reference_date": "2025-11-13"
        if digest == "aa3c0cd17abf6a561a86b8dbb2fbb9253f5991bb6361859a9573e96eef9c49b3"
        else None,
        "date_basis": "hash_pinned_repository_attachment"
        if digest == "aa3c0cd17abf6a561a86b8dbb2fbb9253f5991bb6361859a9573e96eef9c49b3"
        else "unconfirmed",
        "source_url": "https://data.seoul.go.kr/dataList/OA-15964/S/1/datasetView.do",
        "received_at": None,
        "complete": digest == "aa3c0cd17abf6a561a86b8dbb2fbb9253f5991bb6361859a9573e96eef9c49b3",
    }


async def sgis_json(http, token, path, params):
    response = await http.get(
        SGIS_API_BASE + path,
        params={"accessToken": token, **params},
    )
    response.raise_for_status()
    data = response.json()
    if data.get("errCd") != 0:
        raise CollectionError("sgis_provider_error")
    return data


async def collect_areas(http, settings):
    response = await http.get(
        SGIS_API_BASE + "auth/authentication.json",
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
    complete = bool(districts) and len({d["cd"] for d in districts}) == len(districts)
    for district in districts:
        rows = (
            await sgis_json(http, token, "addr/stage.json", {"cd": district["cd"], "pg_yn": "1"})
        )["result"]
        complete = complete and bool(rows)
        for row in rows:
            areas.append((district, row))
    boundary = await sgis_json(
        http,
        token,
        "boundary/hadmarea.geojson",
        {"adm_cd": "11", "year": "2025", "low_search": "0"},
    )
    return {"areas": areas, "boundary": boundary, "complete": complete}


async def collect_sdot(http, settings, *, now=None):
    client = SeoulClient(http, settings.seoul_sdot_api_key.get_secret_value(), "sDoTPeople")
    rows, contexts, pages, issues, seen = [], [], [], [], set()
    now = now or datetime.now(UTC)
    if now.utcoffset() is None:
        raise ValueError("sdot_query_time_requires_timezone")
    hour = now.astimezone(ZoneInfo("Asia/Seoul")).replace(minute=0, second=0, microsecond=0)
    prefixes = [(hour - timedelta(hours=i)).strftime("%Y-%m-%d_%H") for i in range(3)]
    windows = []
    requests = 0
    # Hour-prefix filtering was checked against the public API. Validate every returned row
    # so a provider change cannot silently turn this back into an unbounded/order-dependent read.
    for prefix in prefixes:
        window = {"prefix": prefix, "provider_total": None, "received": 0, "complete": False}
        windows.append(window)
        for start in range(1, 10001, 1000):
            if requests >= 10:
                issues.append("query_page_limit")
                break
            requests += 1
            end = start + 999
            try:
                document = await client.fetch_page_document(start, end, path_params=(prefix,))
                total, page = document["total"], document["rows"]
            except CollectionError as exc:
                pages.append(
                    {
                        "prefix": prefix,
                        "start": start,
                        "end": end,
                        "received": 0,
                        "error_code": str(exc),
                    }
                )
                issues.append(str(exc))
                break
            received = document["received_at"].isoformat()
            if window["provider_total"] is None:
                window["provider_total"] = total
            elif total != window["provider_total"]:
                issues.append("provider_total_changed")
            page_index = len(pages)
            pages.append(
                {
                    "prefix": prefix,
                    "start": start,
                    "end": end,
                    "provider_total": total,
                    "received": len(page),
                    "received_at": received,
                    "body": document["body"],
                }
            )
            if len(page) > 1000:
                pages[-1]["error_code"] = "unexpected_page_size"
                issues.append("unexpected_page_size")
                break
            if any(not str(r.get("SENSING_TIME", "")).startswith(prefix) for r in page):
                pages[-1]["error_code"] = "sensing_time_filter_not_respected"
                issues.append("sensing_time_filter_not_respected")
                break
            duplicate_count = 0
            for index, row in enumerate(page):
                digest = fingerprint(row)
                duplicate_count += int(digest in seen)
                seen.add(digest)
                contexts.append({"page": page_index, "row": index, "received_at": received})
            pages[-1]["duplicate_rows"] = duplicate_count
            rows.extend(page)
            window["received"] += len(page)
            if duplicate_count:
                issues.append("duplicate_page_rows")
            expected = min(1000, max(0, total - start + 1))
            if not page and window["provider_total"] and start <= window["provider_total"]:
                issues.append("unexpected_empty_page")
                break
            if len(page) != expected:
                issues.append("unexpected_page_size")
                break
            if end >= total:
                window["complete"] = window["received"] == window["provider_total"]
                break
    provider_total = (
        sum(w["provider_total"] for w in windows)
        if all(w["provider_total"] is not None for w in windows)
        else None
    )
    return {
        "rows": rows,
        "row_contexts": contexts,
        "pages": pages,
        "provider_total": provider_total,
        "issues": sorted(set(issues)),
        "query": {
            "sensing_time_prefixes": prefixes,
            "windows": windows,
            "max_pages": 10,
            "max_http_attempts": 30,
            "max_rows": 10000,
            "ordering": "client_sensing_time_then_provider_date",
            "snapshot_consistent": False,
        },
        "query_complete": not issues and all(w["complete"] for w in windows),
    }


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
