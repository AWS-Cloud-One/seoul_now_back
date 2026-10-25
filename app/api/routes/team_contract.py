"""Team-facing contract routes; existing collection APIs remain compatible."""

from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query

from app.api.routes.citydata import Session, _summary
from app.api.routes.parking import nearby
from app.api.routes.traffic import roads
from app.repositories.citydata import CityDataRepository

router = APIRouter(tags=["team-contract"])


@router.get("/map/traffic")
async def map_traffic(
    session: Session,
    area_code: str | None = None,
    limit: Annotated[int, Query(ge=1, le=20000)] = 5000,
):
    """Collected hotspot road GeoJSON; coordinates are [longitude, latitude]."""
    return await roads(session, area_code, limit)


@router.get("/map/population")
async def map_population(session: Session, area_code: str | None = None):
    """Source timestamps are retained; these are hotspots, not administrative areas."""
    repository = CityDataRepository(session)
    if area_code:
        snapshot = await repository.get(area_code)
        if snapshot is None:
            raise HTTPException(404, "도시데이터 수집 기록이 없습니다.")
        snapshots = [snapshot]
    else:
        snapshots = await repository.list_latest()
    return {
        "scope": "seoul_citydata_hotspots",
        "items": [
            {
                key: value
                for key, value in _summary(snapshot).items()
                if key in {"area_code", "area_name", "collected_at", "source_time", "population"}
            }
            for snapshot in snapshots
        ],
    }


@router.get("/parkings/nearby")
async def team_nearby(
    session: Session,
    lat: Annotated[float, Query(ge=37.4, le=37.75)],
    lng: Annotated[float, Query(ge=126.7, le=127.25)],
    source: Literal[
        "seoul_getparkinfo", "seoul_citydata_prk", "seoul_municipal_parking", "demo_parking"
    ] = "seoul_getparkinfo",
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
):
    """Team contract uses lng; the legacy endpoint continues accepting lon."""
    result = await nearby(session, lat, lng, source, limit)
    return {
        **result,
        "items": [{**item, "lng": item["lon"]} for item in result["items"]],
    }
