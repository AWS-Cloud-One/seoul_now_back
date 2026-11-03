"""Typed collection prerequisites and official road references; no payload file reads."""

import json

from sqlalchemy import func, select

from app.models import CollectionRun, RawPayload, RegionBoundary, RoadLink


def latest_payload_id(source, target):
    """Select accepted import metadata, including legacy receipts, without loading the file."""
    stamp = func.coalesce(
        RawPayload.received_at, RawPayload.legacy_collected_at, RawPayload.stored_at
    )
    return (
        select(RawPayload.payload_id)
        .join(CollectionRun)
        .where(
            CollectionRun.source == source,
            CollectionRun.target_key == target,
            CollectionRun.status.in_(["success", "partial"]),
            RawPayload.format == "json",
            RawPayload.document_type.in_(["object", "array"]),
            RawPayload.payload_kind.not_in(["legacy_observation", "processing_report"]),
        )
        .order_by(stamp.desc(), RawPayload.stored_at.desc())
        .limit(1)
        .scalar_subquery()
    )


def current_roads():
    return (
        RoadLink.source == "topis_geometry",
        RoadLink.is_active.is_(True),
        RoadLink.last_payload_id == latest_payload_id("topis_geometry", "file:topis_vertices"),
    )


def current_seoul_boundary():
    return (
        RegionBoundary.code_system == "sgis",
        RegionBoundary.region_code == "11",
        RegionBoundary.source == "sgis_seoul_boundary",
        RegionBoundary.is_active.is_(True),
        RegionBoundary.last_payload_id == latest_payload_id("sgis_seoul_boundary", "all"),
    )


class ReferenceRepository:
    def __init__(self, session):
        self.session = session

    async def topis_links(self):
        return list(
            await self.session.scalars(
                select(RoadLink.link_id).where(*current_roads()).order_by(RoadLink.link_id)
            )
        )

    async def topis_attempt_times(self):
        rows = await self.session.execute(
            select(
                CollectionRun.target_key,
                func.coalesce(CollectionRun.finished_at, CollectionRun.started_at),
            )
            .where(CollectionRun.source == "topis_traffic", CollectionRun.target_key.like("link:%"))
            .distinct(CollectionRun.target_key)
            .order_by(
                CollectionRun.target_key, CollectionRun.started_at.desc(), CollectionRun.id.desc()
            )
        )
        return {target.removeprefix("link:"): when.isoformat() for target, when in rows}

    async def seoul_boundary(self):
        row = (
            await self.session.execute(
                select(
                    func.ST_AsGeoJSON(RegionBoundary.geometry), RegionBoundary.source_version
                ).where(*current_seoul_boundary())
            )
        ).first()
        if row is None:
            return None
        year = int(row[1]) if row[1] and row[1].isdigit() else None
        return {"geometry": json.loads(row[0]), "year": year}

    async def has_prerequisite(self, source):
        if source == "topis_geometry:2025":
            query = select(RoadLink.link_id).where(*current_roads())
        elif source == "sgis_seoul_boundary":
            query = select(RegionBoundary.region_code).where(*current_seoul_boundary())
        else:
            raise ValueError("unsupported_collection_prerequisite")
        return bool(await self.session.scalar(select(query.exists())))
