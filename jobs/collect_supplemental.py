"""Bounded external data collection. Run explicitly; HTTP reads never spend ITS quota."""

import argparse
import asyncio
import json
import math
import re
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote
from xml.etree import ElementTree as ET

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.clients.seoul import CollectionError, SeoulClient
from app.core.config import Settings
from app.db.session import create_engine
from app.repositories.provider import save_snapshot

DATA = Path(__file__).parent / "data"


def sensor_locations():
    ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(DATA / "sdot_locations_20251113.xlsx") as book:
        strings = [
            "".join(node.itertext()) for node in ET.fromstring(book.read("xl/sharedStrings.xml"))
        ]
        sheet = ET.fromstring(book.read("xl/worksheets/sheet1.xml"))
        result = {}
        for row in sheet.findall(".//s:row", ns)[1:]:
            values = {}
            for cell in row.findall("s:c", ns):
                node = cell.find("s:v", ns)
                if node is not None:
                    values[re.sub(r"\d+", "", cell.get("r"))] = (
                        strings[int(node.text)] if cell.get("t") == "s" else node.text
                    )
            if not str(values.get("D", "")).startswith("서울"):
                continue
            try:
                code = str(int(values["B"]))
                lat, lng = float(values["E"]), float(values["F"])
                if not (math.isfinite(lat) and math.isfinite(lng)):
                    continue
                result[code] = {"id": code, "address": values["D"], "lat": lat, "lng": lng}
            except KeyError, ValueError, TypeError:
                continue
        return result


def latest_sensors(locations, rows):
    from zoneinfo import ZoneInfo

    result = {
        code: {**value, "count_10min": None, "observed_at": None}
        for code, value in locations.items()
    }
    conflicts = set()
    seen = {}
    for row in rows:
        try:
            code = str(int(row["SERIAL"]))
            when = datetime.fromisoformat(row["SENSING_TIME"].replace("_", "T")).replace(
                tzinfo=ZoneInfo("Asia/Seoul")
            )
            count = int(row["VISITOR_COUNT"])
            if count < 0 or code not in result:
                continue
        except ValueError, KeyError, TypeError:
            continue
        key = (code, when.isoformat())
        if key in seen and seen[key] != count:
            conflicts.add(key)
        seen[key] = count
        item = result[code]
        if not item["observed_at"] or when.isoformat() > item["observed_at"]:
            item.update(count_10min=count, observed_at=when.isoformat())
    for item in result.values():
        if (item["id"], item["observed_at"]) in conflicts:
            item.update(count_10min=None, observation_status="conflicting")
    return list(result.values())


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


async def collect_areas(http, settings, session):
    response = await http.get(
        "https://sgisapi.kostat.go.kr/OpenAPI3/auth/authentication.json",
        params={
            "consumer_key": settings.sgis_consumer_key.get_secret_value(),
            "consumer_secret": settings.sgis_consumer_secret.get_secret_value(),
        },
    )
    response.raise_for_status()
    auth = response.json()
    token = (auth.get("result") or {}).get("accessToken")
    if not token:
        raise CollectionError("sgis_auth_failed")
    districts = (await sgis_json(http, token, "addr/stage.json", {"cd": "11", "pg_yn": "0"}))[
        "result"
    ]
    features = []
    for district in districts:
        rows = (
            await sgis_json(http, token, "addr/stage.json", {"cd": district["cd"], "pg_yn": "1"})
        )["result"]
        for row in rows:
            if not row.get("pg"):
                raise CollectionError("sgis_missing_boundary")
            geometry = await session.scalar(
                text("SELECT ST_AsGeoJSON(ST_Transform(ST_GeomFromText(:wkt,5179),4326))"),
                {"wkt": row["pg"]},
            )
            features.append(
                {
                    "type": "Feature",
                    "id": row["cd"],
                    "geometry": json.loads(geometry),
                    "properties": {
                        "code": row["cd"],
                        "name": row["addr_name"],
                        "full_name": row["full_addr"],
                        "district": district["addr_name"],
                    },
                }
            )
    boundary = await sgis_json(
        http,
        token,
        "boundary/hadmarea.geojson",
        {"adm_cd": "11", "year": "2025", "low_search": "0"},
    )
    for feature in boundary["features"]:
        converted = await session.scalar(
            text(
                "SELECT ST_AsGeoJSON(ST_Transform(ST_SetSRID(ST_GeomFromGeoJSON(:geo),5179),4326))"
            ),
            {"geo": json.dumps(feature["geometry"])},
        )
        feature["geometry"] = json.loads(converted)
    await save_snapshot(
        session,
        "sgis_seoul_boundary",
        {
            "type": "FeatureCollection",
            "features": boundary["features"],
            "boundary_year": 2025,
            "source": "SGIS",
            "notice": "2025 통계 경계 기준 서울 소재지 필터",
        },
    )
    return {
        "type": "FeatureCollection",
        "features": features,
        "source": "SGIS addr/stage",
        "boundary_year": None,
        "notice": "조회 시 제공된 행정동 경계. 제공 응답에 경계 기준연도 없음. SGIS 코드 체계.",
    }


async def collect_sdot(http, settings):
    locations = sensor_locations()
    client = SeoulClient(http, settings.seoul_sdot_api_key.get_secret_value(), "sDoTPeople")
    rows = []
    total = 0
    # Recent rolling window only; never mislabel it as a full historical snapshot.
    for start in range(1, 10001, 1000):
        total, page = await client.parking_page(start, start + 999)
        rows.extend(page)
        if start + 999 >= total:
            break
    return {
        "items": latest_sensors(locations, rows),
        "source": "서울 S-DoT",
        "installation_file_date": "2025-11-13",
        "received": len(rows),
        "provider_total": total,
        "scope": "recent_10000_rows",
        "complete_history": len(rows) >= total,
        "notice": (
            "센서 지점의 10분 통행량이며 주변 체류 인구나 혼잡 등급이 아닙니다. "
            "최근 조회 창에 없는 관측은 미확인입니다."
        ),
    }


async def collect_cctv(http, settings, session, reuse_probe=False):
    from app.models.provider import ProviderSnapshot

    boundary = await session.get(ProviderSnapshot, "sgis_seoul_boundary")
    if boundary is None:
        raise CollectionError("collect_sgis_boundary_first")
    if reuse_probe:
        payload = json.loads((DATA / "its_probe.json").read_text(encoding="utf-8"))
        fetched = datetime.fromtimestamp((DATA / "its_probe.json").stat().st_mtime, UTC)
    else:
        # CLI-only request, no automatic refresh and no retry against limited quota.
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
    items = []
    for row in rows:
        try:
            lng, lat = float(row["coordx"]), float(row["coordy"])
            if not math.isfinite(lng) or not math.isfinite(lat):
                continue
        except ValueError, TypeError, KeyError:
            continue
        inside = await session.scalar(
            text(
                "SELECT ST_Covers(ST_SetSRID(ST_GeomFromGeoJSON(:geo),4326),"
                "ST_SetSRID(ST_Point(:lng,:lat),4326))"
            ),
            {"geo": json.dumps(boundary.data["features"][0]["geometry"]), "lng": lng, "lat": lat},
        )
        if inside:
            items.append(
                {
                    "id": f"{row.get('roadsectionid')}:{len(items)}",
                    "name": row.get("cctvname"),
                    "lat": lat,
                    "lng": lng,
                    "url": row.get("cctvurl"),
                    "format": row.get("cctvformat"),
                    "direction": None,
                    "playback_status": "unverified",
                }
            )
    return {
        "items": items,
        "received": len(rows),
        "source": "ITS CCTV",
        "issued_at": fetched.isoformat(),
        "filter_boundary_year": 2025,
        "notice": (
            "위치·영상 주소 확인 단계. 재생·영상 갱신·촬영 방향은 미검증. "
            "클릭 시 ITS 목록을 재호출하지 않습니다."
        ),
    }


async def collect_parking(http, settings):
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


async def execute(target, reuse_probe=False):
    settings = Settings()
    engine = create_engine(settings)
    source = {
        "areas": "sgis_areas",
        "sdot": "sdot_people",
        "cctv": "its_cctv",
        "parking": "national_parking",
    }[target]
    try:
        async with engine.connect() as lock:
            if not await lock.scalar(
                text("SELECT pg_try_advisory_lock(hashtext(:source))"), {"source": source}
            ):
                return {"source": source, "status": "skipped"}
            try:
                async with httpx.AsyncClient(timeout=45, follow_redirects=True) as http:
                    async with async_sessionmaker(engine).begin() as session:
                        if target == "areas":
                            data = await collect_areas(http, settings, session)
                        elif target == "sdot":
                            from app.models.provider import ProviderSnapshot

                            data = await collect_sdot(http, settings)
                            boundary = await session.get(ProviderSnapshot, "sgis_seoul_boundary")
                            if boundary is None:
                                raise CollectionError("collect_sgis_boundary_first")
                            filtered = []
                            for item in data["items"]:
                                inside = await session.scalar(
                                    text(
                                        "SELECT ST_Covers("
                                        "ST_SetSRID(ST_GeomFromGeoJSON(:geo),4326),"
                                        "ST_SetSRID(ST_Point(:lng,:lat),4326))"
                                    ),
                                    {
                                        "geo": json.dumps(boundary.data["features"][0]["geometry"]),
                                        "lng": item["lng"],
                                        "lat": item["lat"],
                                    },
                                )
                                if inside:
                                    filtered.append(item)
                            data["excluded_outside_seoul"] = len(data["items"]) - len(filtered)
                            data["items"] = filtered
                            data["seoul_boundary_year"] = 2025
                        elif target == "cctv":
                            data = await collect_cctv(http, settings, session, reuse_probe)
                        else:
                            data = await collect_parking(http, settings)
                        await save_snapshot(session, source, data)
                        await save_snapshot(session, source + ":attempt", {"status": "success"})
                        return {
                            "source": source,
                            "status": "success",
                            "records": len(data.get("items", data.get("features", []))),
                        }
            except Exception as exc:
                code = (
                    str(exc) if isinstance(exc, CollectionError) else "provider_or_database_error"
                )
                async with async_sessionmaker(engine).begin() as session:
                    await save_snapshot(
                        session, source + ":attempt", {"status": "failed", "error_code": code}
                    )
                return {"source": source, "status": "failed", "error_code": code}
            finally:
                await lock.execute(
                    text("SELECT pg_advisory_unlock(hashtext(:source))"), {"source": source}
                )
                await lock.commit()
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", choices=["areas", "sdot", "cctv", "parking"])
    parser.add_argument(
        "--reuse-probe",
        action="store_true",
        help="Use already fetched ITS response without spending quota",
    )
    args = parser.parse_args()
    print(json.dumps(asyncio.run(execute(args.target, args.reuse_probe)), ensure_ascii=False))
