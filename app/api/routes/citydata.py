from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_session
from app.repositories.citydata import CityDataRepository

router = APIRouter(prefix="/citydata", tags=["citydata"])
Session = Annotated[AsyncSession, Depends(get_session)]


def _first(value):
    if isinstance(value, list):
        return next((item for item in value if isinstance(item, dict)), {})
    return value if isinstance(value, dict) else {}


def _summary(snapshot) -> dict:
    data = snapshot.data if isinstance(snapshot.data, dict) else {}
    population = _first(data.get("LIVE_PPLTN_STTS"))
    commercial = _first(data.get("LIVE_CMRCL_STTS"))
    road = _first(data.get("ROAD_TRAFFIC_STTS"))
    collection_time = population.get("PPLTN_TIME") or data.get("PPLTN_TIME")
    counts = {
        key: len(value) if isinstance(value, list) else (1 if value else 0)
        for key, value in data.items()
        if key not in {"AREA_NM", "AREA_CD"}
    }
    return {
        "area_code": snapshot.area_code,
        "area_name": snapshot.area_name,
        "collected_at": snapshot.collected_at,
        "source_time": collection_time,
        "population": {
            key: population.get(key)
            for key in (
                "AREA_CONGEST_LVL",
                "AREA_CONGEST_MSG",
                "AREA_PPLTN_MIN",
                "AREA_PPLTN_MAX",
                "PPLTN_TIME",
            )
            if population.get(key) is not None
        },
        "road": road,
        "commercial": commercial,
        "section_counts": counts,
    }


@router.get("/inspect", response_class=HTMLResponse, include_in_schema=False)
def inspect_citydata() -> str:
    return (
        Path(__file__).resolve().parents[2] / "modules/data_pipeline/inspection.html"
    ).read_text(encoding="utf-8")


@router.get("/areas")
async def list_areas(session: Session) -> list[dict]:
    snapshots = await CityDataRepository(session).list_latest()
    return [_summary(snapshot) for snapshot in snapshots]


@router.get("/areas/{area_code}")
async def get_area(area_code: str, session: Session) -> dict:
    snapshot = await CityDataRepository(session).get(area_code)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="도시데이터 수집 기록이 없습니다.")
    return {
        "area_code": snapshot.area_code,
        "area_name": snapshot.area_name,
        "collected_at": snapshot.collected_at,
        "data": snapshot.data,
    }
