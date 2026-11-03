"""Read collection outcomes without spending provider quota."""

from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import select

from app.api.routes.citydata import Session
from app.models.parking import CollectionRun

router = APIRouter(prefix="/collections", tags=["collections"])


@router.get("/runs")
async def list_runs(
    session: Session,
    source: Annotated[str | None, Query(max_length=80)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict:
    statement = select(CollectionRun)
    if source:
        statement = statement.where(CollectionRun.source == source)
    runs = await session.scalars(statement.order_by(CollectionRun.started_at.desc()).limit(limit))
    fields = (
        "id",
        "source",
        "area_code",
        "area_name",
        "started_at",
        "finished_at",
        "status",
        "received",
        "accepted",
        "rejected",
        "duplicates",
        "error_code",
        "rejection_reasons",
    )
    return {"items": [{field: getattr(run, field) for field in fields} for run in runs]}
