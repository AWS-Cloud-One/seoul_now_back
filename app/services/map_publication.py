"""Atomically publish typed canonical data; no provider payload parsing or HTTP."""

import json
from datetime import UTC, datetime

from geoalchemy2 import Geometry
from sqlalchemy import delete, func, select, text, update

from app.models import (
    AdministrativeArea,
    CollectionRun,
    MapFeature,
    MapLayerVersion,
    MapPopulationObservation,
    MapRoadObservation,
    ParkingFacility,
    PopulationArea,
    PopulationLatest,
    RawPayload,
    RoadLink,
    RoadTrafficLatest,
)
from app.modules.data_pipeline.identity import PROCESSOR_VERSION, fingerprint


class MapPublicationService:
    def __init__(self, session):
        self.session = session

    async def publish(self, layer):
        s = self.session
        await s.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": "map-publish:" + layer}
        )
        version = await s.scalar(
            select(MapLayerVersion).where(MapLayerVersion.layer == layer).with_for_update()
        )
        cls = {"roads": RoadLink, "population": PopulationArea, "parking": ParkingFacility}[layer]
        geometry = cls.location.cast(Geometry) if layer == "parking" else cls.geometry
        rows = (
            await s.execute(select(cls, func.ST_AsGeoJSON(geometry)).where(cls.is_active.is_(True)))
        ).all()
        if not rows:
            raise RuntimeError("No normalized geometry available to publish")
        if version is None:
            version = MapLayerVersion(
                layer=layer, geometry_version=None, is_ready=False, coverage=""
            )
            s.add(version)
            await s.flush()
        features = []
        signature = []
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
            signature.append(dict(id=key, geometry=json.loads(geo), properties=props))
        signature.sort(key=lambda f: f["id"])
        version.geometry_version = fingerprint(dict(selection_rule=2, features=signature))
        version.coverage = "Stored provider coverage only; not all Seoul roads or facilities."
        # Dependent publications must be removed before their parent geometry.
        if layer in {"roads", "population"}:
            await s.execute(
                delete(MapRoadObservation if layer == "roads" else MapPopulationObservation)
            )
        await s.execute(delete(MapFeature).where(MapFeature.layer == layer))
        s.add_all(features)
        await s.flush()
        if layer in {"roads", "population"}:
            model = RoadTrafficLatest if layer == "roads" else PopulationLatest
            published = MapRoadObservation if layer == "roads" else MapPopulationObservation
            source = "topis_traffic" if layer == "roads" else "seoul_citydata"
            latest = {
                r.link_id if layer == "roads" else r.area_code: r
                for r in await s.scalars(
                    select(model)
                    .where(model.source.in_([source, "legacy_map_publication"]))
                    .order_by((model.source == source).asc())
                )
            }
            attempts = {
                r.target_key: r
                for r in await s.scalars(
                    select(CollectionRun)
                    .where(
                        CollectionRun.source == source,
                        CollectionRun.run_kind.in_(["collection", "legacy"]),
                    )
                    .distinct(CollectionRun.target_key)
                    .order_by(CollectionRun.target_key, CollectionRun.started_at.desc())
                )
            }
            payload_ids = [r.payload_id for r in latest.values()]
            receipts = (
                dict(
                    (
                        await s.execute(
                            select(RawPayload.payload_id, CollectionRun.started_at)
                            .join(CollectionRun)
                            .where(RawPayload.payload_id.in_(payload_ids))
                        )
                    ).all()
                )
                if payload_ids
                else {}
            )
            for f in features:
                r = latest.get(f.feature_id)
                run = attempts.get(("link:" if layer == "roads" else "area:") + f.feature_id)
                # A newer no-observation response explicitly supersedes old values.
                no_record = bool(
                    run
                    and run.error_code == "provider_no_observation"
                    and (r is None or run.started_at >= receipts[r.payload_id])
                )
                has = r is not None and not no_record
                values = dict(
                    layer=layer,
                    **{"link_id" if layer == "roads" else "area_code": f.feature_id},
                    has_observation=has,
                    last_attempt_id=run.id if run else None,
                    collection_failed=bool(
                        run
                        and run.status == "failed"
                        and not no_record
                        and (r is None or run.started_at >= receipts[r.payload_id])
                    ),
                )
                if has:
                    for name in (
                        "source",
                        "payload_id",
                        "observed_at",
                        "processed_at",
                        "processor_version",
                        "reason_code",
                    ):
                        values[name] = getattr(r, name)
                    for name in (
                        ("speed_kmh", "travel_seconds")
                        if layer == "roads"
                        else ("min_people", "max_people", "congestion", "is_replacement")
                    ):
                        values[name] = getattr(r, name)
                s.add(published(**values))
        version.is_ready = True
        version.updated_at = datetime.now(UTC)
        await s.flush()
        if layer == "roads":
            await s.execute(text("DELETE FROM road_area_mappings"))
            await s.execute(
                text("""INSERT INTO road_area_mappings
                    (link_id,dong_code,boundary_version,road_geometry_version,processor_version,mapped_at)
                SELECT r.link_id,a.dong_code,a.boundary_version,:version,:processor,now()
                FROM road_links r JOIN administrative_areas a
                    ON ST_Intersects(r.geometry,a.geometry)
                WHERE r.is_active AND a.is_active
                    AND ST_Length(ST_Intersection(r.geometry,a.geometry)) > 0"""),
                {"version": version.geometry_version, "processor": PROCESSOR_VERSION},
            )
            await s.execute(
                update(AdministrativeArea)
                .where(AdministrativeArea.is_active.is_(True))
                .values(road_mapping_version=version.geometry_version)
            )
        return len(features)
