"""Read persisted supplemental data without triggering external-provider calls."""

import math
from datetime import UTC, datetime, timedelta
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Query

from app.api.routes.citydata import Session
from app.models.provider import ProviderSnapshot

router = APIRouter(tags=["supplemental-data"])


async def snapshot(session, source):
    record = await session.get(ProviderSnapshot, source)
    if record is None:
        raise HTTPException(503, "해당 데이터가 아직 수집되지 않았습니다.")
    return record


def sensor_status(item, now):
    item = dict(item)
    if item.get("observation_status") == "conflicting":
        return item
    try:
        when = datetime.fromisoformat(item["observed_at"])
        age = now - when
        valid = timedelta(0) <= age <= timedelta(minutes=30)
    except TypeError, ValueError, KeyError:
        valid = False
    item["observation_status"] = "recent" if valid else "stale_or_unobserved"
    if not valid:
        item["last_count_10min"] = item.get("count_10min")
        item["count_10min"] = None
    return item


@router.get("/map/areas")
async def areas(session: Session, district: str | None = None):
    record = await snapshot(session, "sgis_areas")
    features = record.data["features"]
    if district:
        features = [f for f in features if f["properties"]["district"] == district]
    return {**record.data, "features": features, "collected_at": record.collected_at}


@router.get("/map/pedestrians")
async def pedestrians(session: Session):
    record = await snapshot(session, "sdot_people")
    now = datetime.now(UTC)
    return {
        **record.data,
        "items": [sensor_status(item, now) for item in record.data["items"]],
        "collected_at": record.collected_at,
    }


def cctv_item(item, issued_at, now):
    result = dict(item)
    try:
        issued = datetime.fromisoformat(issued_at)
        age = now - issued
        active = timedelta(0) <= age < timedelta(hours=24)
    except TypeError, ValueError:
        active = False
    url = item.get("url") or ""
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        active = False
    result["url"] = url if active else None
    result["url_status"] = "provided_unverified" if active else "expired_or_invalid"
    return result


@router.get("/map/cctv")
async def cctv(session: Session):
    record = await snapshot(session, "its_cctv")
    return {
        **record.data,
        "collected_at": record.collected_at,
        "items": [
            cctv_item(item, record.data.get("issued_at"), datetime.now(UTC))
            for item in record.data["items"]
        ],
    }


@router.get("/parkings/national/nearby")
async def national_parking(
    session: Session,
    lat: Annotated[float, Query(ge=37.4, le=37.75)],
    lng: Annotated[float, Query(ge=126.7, le=127.25)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
):
    record = await snapshot(session, "national_parking")
    datasets = record.data["datasets"]
    operating = {row["prk_center_id"]: row for row in datasets["PrkOprInfo"]["rows"]}
    observed = {row["prk_center_id"]: row for row in datasets["PrkRealtimeInfo"]["rows"]}
    items = {}
    for row in datasets["PrkSttusInfo"]["rows"]:
        if row.get("prk_plce_adres_sido") != "서울특별시":
            continue
        try:
            latitude, longitude = float(row["prk_plce_entrc_la"]), float(row["prk_plce_entrc_lo"])
            if not (37.4 <= latitude <= 37.75 and 126.7 <= longitude <= 127.25):
                continue
        except ValueError, TypeError, KeyError:
            continue
        distance = (
            6371000
            * 2
            * math.asin(
                min(
                    1,
                    math.sqrt(
                        math.sin(math.radians(latitude - lat) / 2) ** 2
                        + math.cos(math.radians(lat))
                        * math.cos(math.radians(latitude))
                        * math.sin(math.radians(longitude - lng) / 2) ** 2
                    ),
                )
            )
        )
        code = row["prk_center_id"]
        items[code] = {
            "id": code,
            "name": row["prk_plce_nm"],
            "address": row.get("prk_plce_adres"),
            "lat": latitude,
            "lon": longitude,
            "lng": longitude,
            "distance_m": round(distance),
            "registered_details": row,
            "operating_details": operating.get(code),
            "unverified_occupancy": observed.get(code),
            "real_time_available": False,
            "available_spaces": None,
            "collected_at": record.collected_at,
        }
    return {
        "items": sorted(items.values(), key=lambda v: (v["distance_m"], v["id"]))[:limit],
        "source": record.data["source"],
        "scope": record.data["scope"],
        "notice": record.data["notice"],
        "collected_at": record.collected_at,
        "collection_complete": {key: value["complete"] for key, value in datasets.items()},
    }


@router.get("/map/population/geometry")
async def population_geometry(session: Session):
    from app.api.routes.citydata import _first
    from app.modules.data_pipeline.normalizers.parking import source_time
    from app.repositories.citydata import CityDataRepository

    geometry = await snapshot(session, "citydata_place_boundaries")
    observations = {r.area_code: r for r in await CityDataRepository(session).list_latest()}
    features = []
    now = datetime.now(UTC)
    for feature in geometry.data["features"]:
        props = {**feature["properties"], "status": "unobserved", "congestion": None}
        observation = observations.get(feature["id"])
        if observation:
            population = _first(observation.data.get("LIVE_PPLTN_STTS"))
            when = source_time(population.get("PPLTN_TIME"))
            replaced = str(population.get("REPLACE_YN", "")).upper()
            valid = when and timedelta(0) <= now - when <= timedelta(hours=1)
            props.update(
                source_time=population.get("PPLTN_TIME"),
                collected_at=observation.collected_at.isoformat(),
                replacement=replaced,
                population_min=population.get("AREA_PPLTN_MIN"),
                population_max=population.get("AREA_PPLTN_MAX"),
            )
            props["status"] = (
                "replacement"
                if replaced == "Y"
                else ("recent" if valid and replaced == "N" else "stale_or_unconfirmed")
            )
            if props["status"] == "recent":
                props["congestion"] = population.get("AREA_CONGEST_LVL")
        features.append({**feature, "properties": props})
    return {
        **geometry.data,
        "features": features,
        "collected_at": geometry.collected_at,
        "notice": (
            "장소별 추정 체류 인구. 1시간 이내 원본 시각·대체 아님 확인값만 색상 표시. "
            "회색은 미확인·지연·대체값."
        ),
    }
