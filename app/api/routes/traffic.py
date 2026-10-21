from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_session
from app.models.provider import ProviderSnapshot
from app.modules.data_pipeline.normalizers.traffic import road_feature
from app.repositories.citydata import CityDataRepository

router = APIRouter(prefix="/traffic", tags=["traffic"])
Session = Annotated[AsyncSession, Depends(get_session)]


@router.get("/roads")
async def roads(
    session: Session,
    area_code: str | None = None,
    limit: Annotated[int, Query(ge=1, le=20000)] = 5000,
):
    repository = CityDataRepository(session)
    snapshots = [await repository.get(area_code)] if area_code else await repository.list_latest()
    features = {}
    omitted = 0
    for snapshot in sorted((s for s in snapshots if s is not None), key=lambda s: s.collected_at):
        traffic = snapshot.data.get("ROAD_TRAFFIC_STTS") or {}
        if isinstance(traffic, list):
            traffic = next((r for r in traffic if isinstance(r, dict)), {})
        if not isinstance(traffic, dict):
            continue
        avg = traffic.get("AVG_ROAD_DATA") or {}
        for row in traffic.get("ROAD_TRAFFIC_STTS") or []:
            if not isinstance(row, dict):
                omitted += 1
                continue
            feature = road_feature(
                row,
                snapshot.area_code,
                snapshot.collected_at.isoformat(),
                avg.get("ROAD_TRAFFIC_TIME") if isinstance(avg, dict) else None,
            )
            if feature is None:
                omitted += 1
            else:
                features[feature["id"]] = feature
    ordered = sorted(features.values(), key=lambda f: f["id"])
    return {
        "type": "FeatureCollection",
        "features": ordered[:limit],
        "total": len(ordered),
        "truncated": len(ordered) > limit,
        "omitted_invalid_geometry": omitted,
        "scope": "Collected Seoul citydata hotspots, not all Seoul roads",
    }


@router.get("/topis")
async def topis_list(session: Session):
    from sqlalchemy import select

    snapshots = await session.scalars(
        select(ProviderSnapshot)
        .where(ProviderSnapshot.source.like("topis:%"))
        .order_by(ProviderSnapshot.source)
    )
    return [{"source": s.source, "collected_at": s.collected_at, "data": s.data} for s in snapshots]


@router.get("/topis/{link_id}")
async def topis(link_id: str, session: Session):
    record = await session.get(ProviderSnapshot, "topis:" + link_id)
    if record is None:
        raise HTTPException(404, "TOPIS 수집 기록이 없습니다.")
    return {
        "link_id": link_id,
        "source": "TOPIS TrafficInfo",
        "collected_at": record.collected_at,
        "data": record.data,
    }


@router.get("/mapping-report")
async def mapping_report(session: Session):
    geometry = await session.get(ProviderSnapshot, "topis_geometry:2025")
    if geometry is None:
        raise HTTPException(503, "공식 도로선과 매핑 자료가 아직 적재되지 않았습니다.")
    return {"imported_at": geometry.collected_at, "report": geometry.data["report"]}


@router.get("/network")
async def network(
    session: Session,
    area_code: str | None = None,
    limit: Annotated[int, Query(ge=1, le=10000)] = 500,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    from collections import Counter

    from sqlalchemy import select

    from app.modules.data_pipeline.normalizers.traffic_network import network_feature

    geometry = await session.get(ProviderSnapshot, "topis_geometry:2025")
    if geometry is None:
        raise HTTPException(503, "공식 도로선 자료가 아직 적재되지 않았습니다.")
    features = geometry.data["features"]
    if area_code:
        area = await CityDataRepository(session).get(area_code)
        if area is None:
            raise HTTPException(404, "수집되지 않은 도시데이터 장소입니다.")
        roads = (area.data.get("ROAD_TRAFFIC_STTS") or {}).get("ROAD_TRAFFIC_STTS") or []
        standard_ids = {
            str(row["LINK_ID"]) for row in roads if isinstance(row, dict) and row.get("LINK_ID")
        }
        inverse = geometry.data["standard_to_services"]
        service_ids = {link for standard in standard_ids for link in inverse.get(standard, [])}
        features = [f for f in features if f["id"] in service_ids]
    observations = await session.scalars(
        select(ProviderSnapshot).where(ProviderSnapshot.source.like("topis:%"))
    )
    observed = {r.source.removeprefix("topis:"): r for r in observations}
    attempts = await session.scalars(
        select(ProviderSnapshot).where(ProviderSnapshot.source.like("topis_attempt:%"))
    )
    attempted = {r.source.removeprefix("topis_attempt:"): r for r in attempts}
    joined = [network_feature(f, observed.get(f["id"]), attempted.get(f["id"])) for f in features]
    return {
        "type": "FeatureCollection",
        "features": joined[offset : offset + limit],
        "total": len(joined),
        "offset": offset,
        "next_offset": offset + limit if offset + limit < len(joined) else None,
        "status_counts": dict(Counter(f["properties"]["observation_status"] for f in joined)),
        "geometry_year": 2025,
        "mapping_source": "OA-15061 2025-03-06",
        "notice": (
            "Speed belongs to the TOPIS service link. "
            "Source measurement timestamp is not supplied; "
            "traffic_collected_at is collection time. "
            "Geometry is the published 2025 version."
        ),
    }


@router.get("/collection-status")
async def collection_status(session: Session):
    snapshot = await session.get(ProviderSnapshot, "topis_collection_status")
    if snapshot is None:
        raise HTTPException(404, "전체 수집 실행 이력이 없습니다.")
    return {"updated_at": snapshot.collected_at, **snapshot.data}
