"""Publish committed canonical data atomically; refresh observations without rebuilding geometry."""

import json
from datetime import UTC, datetime

from geoalchemy2 import Geometry
from sqlalchemy import delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert

from app.models import (
    MapFeature,
    MapLayerVersion,
    MapPopulationObservation,
    MapRoadObservation,
    ParkingFacility,
    PopulationArea,
    PopulationAttemptLatest,
    PopulationLatest,
    RoadLink,
    RoadTrafficAttemptLatest,
    RoadTrafficLatest,
)
from app.modules.data_pipeline.identity import fingerprint
from app.repositories.map import lock_map_layer
from app.repositories.reference import current_roads
from app.repositories.road_reference import lock_road_reference, rebuild_road_areas


class PublicationNotReady(RuntimeError):
    def __init__(self, code, message=None):
        self.code = code
        super().__init__(message or code)


class MapPublicationService:
    def __init__(self, session):
        self.session = session
        self.geometry_changed = False
        self.observations_changed = 0

    async def _lock(self, layer):
        self.geometry_changed = False
        self.observations_changed = 0
        if layer not in {"roads", "population", "parking"}:
            raise ValueError("unsupported_map_layer")
        if layer == "roads":
            await self.session.run_sync(lock_road_reference)
        await self.session.run_sync(lambda sync: lock_map_layer(sync, layer))
        return await self.session.scalar(
            select(MapLayerVersion).where(MapLayerVersion.layer == layer).with_for_update()
        )

    @staticmethod
    def _canonical(layer):
        cls = {"roads": RoadLink, "population": PopulationArea, "parking": ParkingFacility}[layer]
        geometry = cls.location.cast(Geometry) if layer == "parking" else cls.geometry
        filters = current_roads() if layer == "roads" else (cls.is_active.is_(True),)
        return cls, geometry, filters

    async def publish(self, layer):
        """Explicit reference refresh/retry; unchanged geometry retains its rows and timestamps."""
        s = self.session
        version = await self._lock(layer)
        cls, geometry, filters = self._canonical(layer)
        rows = (await s.execute(select(cls, func.ST_AsGeoJSON(geometry)).where(*filters))).all()
        if not rows:
            raise PublicationNotReady(
                "missing_normalized_geometry", "No normalized geometry available to publish"
            )
        if version is None:
            version = MapLayerVersion(
                layer=layer, geometry_version=None, is_ready=False, coverage=""
            )
            s.add(version)
            await s.flush()
        features, signature = [], []
        geometries = {}
        for r, geo in rows:
            key = (
                str(r.parking_id)
                if layer == "parking"
                else r.link_id
                if layer == "roads"
                else r.area_code
            )
            values = dict(
                layer=layer,
                feature_id=key,
                name=r.name,
                geometry=func.ST_SetSRID(func.ST_GeomFromGeoJSON(geo), 4326),
                is_active=True,
            )
            props = dict(name=r.name)
            if layer == "roads":
                values.update(road_link_id=key, direction=r.direction)
                props.update(link_id=key, direction=r.direction)
            elif layer == "population":
                values["population_area_code"] = key
                props["area_code"] = key
            else:
                values.update(parking_id=r.parking_id, identity_status=r.identity_status)
                props.update(parking_id=key, identity_status=r.identity_status)
            features.append(MapFeature(**values))
            geometries[key] = json.loads(geo)
            signature.append(dict(id=key, geometry=json.loads(geo), properties=props))
        signature.sort(key=lambda f: f["id"])
        geometry_version = fingerprint(dict(selection_rule=2, features=signature))
        self.geometry_changed = not version.is_ready or version.geometry_version != geometry_version
        if self.geometry_changed:
            await self._write_features(layer, features, geometries)
            version.geometry_version = geometry_version
            version.coverage = "Stored provider coverage only; not all Seoul roads or facilities."
            version.is_ready = True
            if layer == "roads":
                await s.run_sync(
                    lambda sync: rebuild_road_areas(
                        sync, [f.feature_id for f in features], geometry_version
                    )
                )
        if layer in {"roads", "population"}:
            await self._write_observations(layer, [f.feature_id for f in features])
        await self._finish(version)
        return len(features)

    async def _write_features(self, layer, features, geometries):
        """Apply only changed features; a transaction publishes the entire new version at once."""
        s = self.session
        existing = {
            row.feature_id: (row, json.loads(geo))
            for row, geo in await s.execute(
                select(MapFeature, func.ST_AsGeoJSON(MapFeature.geometry)).where(
                    MapFeature.layer == layer
                )
            )
        }
        removed = set(existing) - {feature.feature_id for feature in features}
        if removed:
            if layer in {"roads", "population"}:
                observation = self._observation_model(layer)
                identity = observation.link_id if layer == "roads" else observation.area_code
                await s.execute(delete(observation).where(identity.in_(removed)))
            await s.execute(
                delete(MapFeature).where(
                    MapFeature.layer == layer, MapFeature.feature_id.in_(removed)
                )
            )
        attributes = (
            "name",
            "direction",
            "identity_status",
            "is_active",
            "road_link_id",
            "population_area_code",
            "parking_id",
        )
        for incoming in features:
            old = existing.get(incoming.feature_id)
            if old is None:
                s.add(incoming)
                continue
            current, geometry = old
            changed = geometry != geometries[incoming.feature_id]
            if changed:
                current.geometry = incoming.geometry
            for name in attributes:
                if getattr(current, name) != getattr(incoming, name):
                    setattr(current, name, getattr(incoming, name))
                    changed = True
            if changed:
                current.published_at = datetime.now(UTC)
        await s.flush()

    async def refresh_observations(self, layer):
        """Only refresh a ready publication whose canonical reference still matches."""
        if layer not in {"roads", "population"}:
            raise ValueError("unsupported_observation_layer")
        version = await self._lock(layer)
        if version is None or not version.is_ready or not version.geometry_version:
            raise PublicationNotReady("geometry_publication_required")
        s = self.session
        ids = list(await s.scalars(select(MapFeature.feature_id).where(MapFeature.layer == layer)))
        cls, geometry, filters = self._canonical(layer)
        key = cls.link_id if layer == "roads" else cls.area_code
        # Compare in PostgreSQL; no GeoJSON transfer, geometry writes or spatial remapping.
        matches = [
            MapFeature.feature_id.is_(None),
            func.ST_AsGeoJSON(geometry).is_distinct_from(func.ST_AsGeoJSON(MapFeature.geometry)),
            cls.name.is_distinct_from(MapFeature.name),
        ]
        if layer == "roads":
            matches.append(cls.direction.is_distinct_from(MapFeature.direction))
        different = (
            select(key)
            .outerjoin(MapFeature, (MapFeature.layer == layer) & (MapFeature.feature_id == key))
            .where(*filters, or_(*matches))
        )
        count = await s.scalar(select(func.count()).select_from(cls).where(*filters))
        if count != len(ids) or await s.scalar(select(different.exists())):
            raise PublicationNotReady("geometry_publication_required")
        await self._write_observations(layer, ids)
        await self._finish(version)
        return len(ids)

    @staticmethod
    def _observation_model(layer):
        return MapRoadObservation if layer == "roads" else MapPopulationObservation

    async def _write_observations(self, layer, ids):
        if not ids:
            return
        s = self.session
        model = RoadTrafficLatest if layer == "roads" else PopulationLatest
        published = self._observation_model(layer)
        key = "link_id" if layer == "roads" else "area_code"
        source = "topis_traffic" if layer == "roads" else "seoul_citydata"
        latest = {
            getattr(r, key): r
            for r in await s.scalars(
                select(model)
                .where(
                    model.source.in_([source, "legacy_map_publication"]),
                    getattr(model, key).in_(ids),
                )
                .order_by((model.source == source).asc())
            )
        }
        attempt_model = RoadTrafficAttemptLatest if layer == "roads" else PopulationAttemptLatest
        attempts = {
            getattr(row, key): row
            for row in await s.scalars(
                select(attempt_model).where(getattr(attempt_model, key).in_(ids))
            )
        }
        measurements = (
            ("speed_kmh", "travel_seconds")
            if layer == "roads"
            else ("min_people", "max_people", "congestion", "is_replacement")
        )
        evidence = (
            "source",
            "observed_at",
            "processed_at",
            "processor_version",
            "reason_code",
        )
        self.observations_changed = 0
        for identity in ids:
            r, current = latest.get(identity), attempts.get(identity)
            receipt = r.received_at if r else None
            newer_attempt = bool(
                current
                and (
                    r is None
                    or (
                        not current.observation_updated
                        and (receipt is None or current.attempted_at >= receipt)
                    )
                )
            )
            attempt_id = current.run_id if current and layer == "roads" else None
            attempt_status = current.status if current else None
            attempt_error = current.error_code if current else None
            no_record = bool(newer_attempt and attempt_error == "provider_no_observation")
            has = r is not None and not no_record
            values = dict(
                layer=layer,
                **{key: identity},
                has_observation=has,
                last_attempt_id=attempt_id,
                received_at=receipt if has else None,
                attempted_at=current.attempted_at if current else None,
                attempt_status=attempt_status,
                attempt_error_code=attempt_error,
                # A completed but rejected refresh retains the prior value and its receipt.
                # Partial errors in another CITYDATA section do not taint this run's valid value.
                collection_failed=bool(
                    newer_attempt and attempt_status in {"failed", "partial"} and not no_record
                ),
                **{name: getattr(r, name) if has else None for name in (*evidence, *measurements)},
            )
            statement = insert(published).values(**values)
            updates = {
                name: statement.excluded[name] for name in values if name not in {key, "layer"}
            }
            changed = or_(
                *(
                    getattr(published, name).is_distinct_from(value)
                    for name, value in updates.items()
                )
            )
            result = await s.execute(
                statement.on_conflict_do_update(
                    index_elements=[key],
                    set_={**updates, "published_at": func.now()},
                    where=changed,
                ).returning(getattr(published, key))
            )
            self.observations_changed += len(result.all())

    async def _finish(self, version):
        if self.geometry_changed or self.observations_changed:
            version.updated_at = datetime.now(UTC)
        await self.session.flush()
