from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_session
from app.models.parking import CollectionRun
from app.modules.data_pipeline.normalizers.parking import SOURCE
from app.repositories.parking import ParkingRepository

router = APIRouter(prefix="/parking", tags=["parking"])
Session = Annotated[AsyncSession, Depends(get_session)]


@router.get("/inspect", response_class=HTMLResponse, include_in_schema=False)
def inspect_parking() -> str:
    """Local data inspection utility, not the team's product frontend."""
    return (
        Path(__file__).resolve().parents[2] / "modules/data_pipeline/inspection.html"
    ).read_text(encoding="utf-8")


@router.get("/nearby")
async def nearby(
    session: Session,
    lat: Annotated[float, Query(ge=37.4, le=37.75)],
    lon: Annotated[float, Query(ge=126.7, le=127.25)],
    source: Literal[
        "seoul_getparkinfo", "seoul_citydata_prk", "seoul_municipal_parking", "demo_parking"
    ] = SOURCE,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict:
    """Search nearby parking; expose only fresh, provider-confirmed vacancies."""
    return await ParkingRepository(session).nearby(lat, lon, source, limit)


@router.get("/runs")
async def runs(
    session: Session,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[dict]:
    records = await session.scalars(
        select(CollectionRun).order_by(CollectionRun.started_at.desc()).limit(limit)
    )
    return [
        {column.name: getattr(run, column.name) for column in CollectionRun.__table__.columns}
        for run in records
    ]


@router.get("/municipal/raw")
async def municipal_raw(session: Session):
    from fastapi import HTTPException

    from app.models.provider import ProviderSnapshot

    snapshot = await session.get(ProviderSnapshot, "seoul_municipal_parking")
    if snapshot is None:
        raise HTTPException(404, "시영주차장 수집 기록이 없습니다.")
    return {"source": snapshot.source, "collected_at": snapshot.collected_at, "data": snapshot.data}
