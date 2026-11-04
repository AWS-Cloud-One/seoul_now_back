"""Spatial lookup within the API's committed snapshot; no reader row locks."""

from dataclasses import dataclass

from sqlalchemy import func, select

from app.models import (
    MapFeature,
    MapLayerVersion,
    MapPopulationObservation,
    MapRoadObservation,
)


def lock_map_layer(session, layer):
    session.execute(select(func.pg_advisory_xact_lock(func.hashtext("map-publish:" + layer))))


@dataclass(frozen=True)
class SelectedFeature:
    feature_id: str
    properties: dict
    geometry: str | None


@dataclass(frozen=True)
class Observation:
    value: dict | None
    observed_at: object
    collected_at: object
    collection_failed: bool


class MapRepository:
    def __init__(self, session):
        self.session = session

    async def get_version(self, layer):
        return await self.session.scalar(
            select(MapLayerVersion).where(MapLayerVersion.layer == layer)
        )

    async def select_features(self, layer, bbox, max_features, *, include_geometry):
        cols = [MapFeature]
        if include_geometry:
            cols.append(func.ST_AsGeoJSON(MapFeature.geometry))
        rows = await self.session.execute(
            select(*cols)
            .where(
                MapFeature.layer == layer,
                MapFeature.is_active.is_(True),
                func.ST_Intersects(MapFeature.geometry, func.ST_MakeEnvelope(*bbox, 4326)),
            )
            .order_by(MapFeature.feature_id)
            .limit(max_features + 1)
        )
        results = []
        for row in rows:
            f = row[0]
            properties = {"name": f.name}
            if layer == "roads":
                properties.update(link_id=f.road_link_id, direction=f.direction)
            elif layer == "population":
                properties["area_code"] = f.population_area_code
            else:
                properties.update(parking_id=str(f.parking_id), identity_status=f.identity_status)
            results.append(
                SelectedFeature(f.feature_id, properties, row[1] if include_geometry else None)
            )
        return results

    async def get_observations(self, layer, ids):
        if not ids:
            return {}
        cls = MapRoadObservation if layer == "roads" else MapPopulationObservation
        key = cls.link_id if layer == "roads" else cls.area_code
        rows = await self.session.scalars(select(cls).where(key.in_(ids)))
        result = {}
        for r in rows:
            value = None
            if r.has_observation:
                value = (
                    {
                        k: float(getattr(r, k)) if getattr(r, k) is not None else None
                        for k in ("speed_kmh", "travel_seconds")
                    }
                    if layer == "roads"
                    else {
                        k: getattr(r, k)
                        for k in ("min_people", "max_people", "congestion", "is_replacement")
                    }
                )
            result[r.link_id if layer == "roads" else r.area_code] = Observation(
                value, r.observed_at, r.received_at, r.collection_failed
            )
        return result
