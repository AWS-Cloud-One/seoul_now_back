"""Map DB access. Geometry and status share one locked version and selection rule."""

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.map import MapFeature, MapLayerVersion, MapObservation


@dataclass(frozen=True)
class SelectedFeature:
    feature_id: str
    properties: dict
    geometry: str | None


class MapRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_version(self, layer: str) -> MapLayerVersion | None:
        # Publisher takes FOR UPDATE on this row. Keep this shared lock until
        # the request session closes, including subsequent observation queries.
        return await self.session.scalar(
            select(MapLayerVersion).where(MapLayerVersion.layer == layer).with_for_update(read=True)
        )

    async def select_features(self, layer, bbox, max_features, *, include_geometry):
        columns = [MapFeature.feature_id, MapFeature.properties]
        if include_geometry:
            columns.append(func.ST_AsGeoJSON(MapFeature.geometry))
        rows = await self.session.execute(
            select(*columns)
            .where(
                MapFeature.layer == layer,
                MapFeature.is_active.is_(True),
                func.ST_Intersects(MapFeature.geometry, func.ST_MakeEnvelope(*bbox, 4326)),
            )
            .order_by(MapFeature.feature_id)
            .limit(max_features + 1)
        )
        return [
            SelectedFeature(row[0], row[1], row[2] if include_geometry else None) for row in rows
        ]

    async def get_observations(self, layer, ids):
        if not ids:
            return {}
        rows = await self.session.scalars(
            select(MapObservation).where(
                MapObservation.layer == layer,
                MapObservation.feature_id.in_(ids),
            )
        )
        return {row.feature_id: row for row in rows}
