"""Publish collected data to API_SPEC map tables; never calls external providers.

Run after collectors/importers: python -m jobs.publish_map roads|population|parking.
Each publication is atomic, and an unchanged geometry keeps its version.
"""

import argparse
import asyncio
import hashlib
import json
import math
from uuid import uuid4

from geoalchemy2 import Geometry
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import Settings
from app.db.session import create_engine
from app.models.citydata import CityDataSnapshot
from app.models.map import (
    MapFeature,
    MapLayerVersion,
    MapObservation,
    ParkingFacility,
    ParkingSourceRecord,
)
from app.models.parking import CollectionRun, ParkingRecord
from app.models.provider import ProviderSnapshot
from app.modules.data_pipeline.normalizers.parking import source_time


def number(raw, integer=False):
    try:
        value = float(raw)
        if not math.isfinite(value) or value < 0 or (integer and not value.is_integer()):
            return None
        return int(value) if integer else value
    except TypeError, ValueError:
        return None


def registered(record):
    details = record.details or {}
    capacity = number(details.get("TPKCT", details.get("CPCTY")), integer=True)

    def fee(*keys):
        for key in keys:
            value = number(details.get(key))
            if value is not None:
                return value if value > 0 else None
        return None

    fees = {
        "base_minutes": fee("PRK_HM", "TIME_RATES"),
        "base_fee_krw": fee("PRK_CRG", "RATES"),
        "additional_minutes": fee("ADD_UNIT_TM_MNT", "ADD_TIME_RATES"),
        "additional_fee_krw": fee("ADD_CRG", "ADD_RATES"),
        "daily_max_krw": None,
        "notes": None,
    }

    def hours(start, end):
        a, b = details.get(start), details.get(end)
        if a is None or b is None or not str(a).strip() or not str(b).strip():
            return None
        return f"{a}~{b}"

    hours_value = {
        "weekdays": hours("WD_OPER_BGNG_TM", "WD_OPER_END_TM"),
        "saturday": hours("WE_OPER_BGNG_TM", "WE_OPER_END_TM"),
        "sunday_holidays": hours("LHLDY_BGNG", "LHLDY"),
        "notes": None,
    }
    # A collector's live vacancy timestamp is not a registration update timestamp.
    updated = record.source_updated_at if record.source == "seoul_getparkinfo" else None
    return {
        "value": {
            "address": record.address,
            "registered_capacity": capacity,
            "fees": fees if any(v is not None for v in fees.values()) else None,
            "operating_hours": hours_value
            if any(v is not None for v in hours_value.values())
            else None,
        },
        "status": "available",
        "freshness": "unknown",
        "observed_at": updated.isoformat() if updated else None,
        "collected_at": record.collected_at.isoformat(),
        "source": record.source,
        "reason_code": "SOURCE_TIME_UNKNOWN",
    }


async def build_parking(session, publication_info=None):
    runs = (
        await session.scalars(
            select(CollectionRun)
            .where(
                CollectionRun.source.in_(
                    [
                        "seoul_getparkinfo",
                        "seoul_citydata_prk",
                        "seoul_municipal_parking",
                    ]
                ),
                CollectionRun.status.in_(["success", "partial"]),
                CollectionRun.finished_at.is_not(None),
                CollectionRun.accepted > 0,
            )
            .order_by(CollectionRun.started_at.desc())
        )
    ).all()
    latest = {}
    for run in runs:
        # Area snapshots must not replace other areas of the same provider.
        latest.setdefault((run.source, run.area_code), run)
    if not latest:
        raise RuntimeError("No completed parking collection with accepted records to publish")
    partial_sources = {run.source for run in latest.values() if run.status == "partial"}
    if publication_info is not None:
        publication_info.update(
            partial_runs=sum(run.status == "partial" for run in latest.values()),
            rejected_rows=sum(run.rejected for run in latest.values()),
        )
    mappings = {r.legacy_record_id: r for r in await session.scalars(select(ParkingSourceRecord))}
    selected = {}
    for (source, _area), run in latest.items():
        rows = await session.execute(
            select(
                ParkingRecord,
                func.ST_AsGeoJSON(ParkingRecord.location.cast(Geometry)),
            ).where(ParkingRecord.source == source, ParkingRecord.collected_at == run.started_at)
        )
        for record, shape in rows:
            selected[record.id] = (record, shape)
    features = []
    for record, shape in selected.values():
        mapping = mappings.get(record.id)
        if mapping is None:
            facility = ParkingFacility(id=uuid4(), identity_status="unverified", is_active=True)
            session.add(facility)
            await session.flush()
            mapping = ParkingSourceRecord(
                id=uuid4(),
                parking_id=facility.id,
                legacy_record_id=record.id,
                source=record.source,
                reference_date=None,
                registered=registered(record),
                is_active=True,
            )
            session.add(mapping)
        else:
            mapping.registered, mapping.is_active = registered(record), True
            facility = await session.get(ParkingFacility, mapping.parking_id)
            facility.is_active = True
        features.append(
            {
                "id": str(mapping.parking_id),
                "geometry": json.loads(shape),
                "properties": {
                    "parking_id": str(mapping.parking_id),
                    "name": record.name,
                    "identity_status": facility.identity_status,
                },
            }
        )
    for key, mapping in mappings.items():
        if key not in selected:
            if mapping.source in partial_sources:
                # Rejected records are not evidence that an existing facility
                # closed. Keep previously published geometry and original dates.
                previous = await session.get(MapFeature, ("parking", str(mapping.parking_id)))
                if mapping.is_active and previous is not None and previous.is_active:
                    shape = await session.scalar(
                        select(func.ST_AsGeoJSON(MapFeature.geometry)).where(
                            MapFeature.layer == "parking",
                            MapFeature.feature_id == str(mapping.parking_id),
                        )
                    )
                    features.append(
                        {
                            "id": str(mapping.parking_id),
                            "geometry": json.loads(shape),
                            "properties": previous.properties,
                        }
                    )
                continue
            mapping.is_active = False
            facility = await session.get(ParkingFacility, mapping.parking_id)
            # The publisher creates one unverified facility per source record.
            # Manually verified multi-source facilities require a separate publisher.
            if facility.identity_status == "unverified":
                facility.is_active = False
    return features, []


async def build_roads(session):
    snapshot = await session.get(ProviderSnapshot, "topis_geometry:2025")
    if snapshot is None:
        raise RuntimeError("Import TOPIS geometry before publishing")
    features = [
        {
            "id": str(f["id"]),
            "geometry": f["geometry"],
            "properties": {
                "link_id": str(f["id"]),
                "name": f["properties"].get("name"),
                "direction": f["properties"].get("direction"),
            },
        }
        for f in snapshot.data["features"]
    ]
    snapshots = {
        r.source: r
        for r in await session.scalars(
            select(ProviderSnapshot).where(
                ProviderSnapshot.source.like("topis:%")
                | ProviderSnapshot.source.like("topis_attempt:%"),
            )
        )
    }
    observations = []
    for feature in features:
        key = feature["id"]
        observed = snapshots.get("topis:" + key)
        attempt = snapshots.get("topis_attempt:" + key)
        failed = bool(
            attempt
            and attempt.data.get("status") == "failed"
            and (observed is None or attempt.collected_at >= observed.collected_at)
        )
        rows = [
            r
            for r in (observed.data.get("rows", []) if observed else [])
            if str(r.get("link_id")) == key
        ]
        value = None
        if len(rows) == 1:
            value = {
                "speed_kmh": number(rows[0].get("prcs_spd")),
                "travel_seconds": number(rows[0].get("prcs_trv_time")),
            }
        if (
            attempt
            and attempt.data.get("error_code") == "provider_no_observation"
            and (observed is None or attempt.collected_at >= observed.collected_at)
        ):
            value, failed = None, False
        observations.append(
            MapObservation(
                layer="roads",
                feature_id=key,
                value=value,
                observed_at=None,
                collected_at=observed.collected_at if observed else None,
                collection_failed=failed,
            )
        )
    return features, observations


async def build_population(session):
    boundary = await session.get(ProviderSnapshot, "citydata_place_boundaries")
    if boundary is None:
        raise RuntimeError("Import official population boundaries before publishing")
    features = [
        {
            "id": str(f["id"]),
            "geometry": f["geometry"],
            "properties": {"area_code": str(f["id"]), "name": f["properties"]["name"]},
        }
        for f in boundary.data["features"]
    ]
    snapshots = {r.area_code: r for r in await session.scalars(select(CityDataSnapshot))}
    runs = await session.scalars(
        select(CollectionRun)
        .where(
            CollectionRun.source == "seoul_citydata",
            CollectionRun.status != "running",
        )
        .order_by(CollectionRun.started_at.desc())
    )
    latest = {}
    for run in runs:
        latest.setdefault(run.area_code or run.area_name, run)
    congestion = {
        "여유": "relaxed",
        "보통": "normal",
        "약간 붐빔": "slightly_crowded",
        "붐빔": "crowded",
    }
    observations = []
    for f in features:
        snapshot = snapshots.get(f["id"])
        rows = snapshot.data.get("LIVE_PPLTN_STTS") if snapshot else None
        row = rows[0] if isinstance(rows, list) and rows else (rows or {})
        run = latest.get(f["id"]) or latest.get(f["properties"]["name"])
        failed = bool(
            run
            and run.status == "failed"
            and (snapshot is None or run.started_at >= snapshot.collected_at)
        )
        observations.append(
            MapObservation(
                layer="population",
                feature_id=f["id"],
                value={
                    "min_people": number(row.get("AREA_PPLTN_MIN"), True),
                    "max_people": number(row.get("AREA_PPLTN_MAX"), True),
                    "congestion": congestion.get(row.get("AREA_CONGEST_LVL")),
                    "is_replacement": {"Y": True, "N": False}.get(row.get("REPLACE_YN")),
                }
                if row
                else None,
                observed_at=source_time(row.get("PPLTN_TIME")),
                collected_at=snapshot.collected_at if snapshot else None,
                collection_failed=failed,
            )
        )
    return features, observations


async def publish(session, layer):
    # Serializes publishers and protects readers even on the first publication.
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": "map-publish:" + layer}
    )
    version = await session.scalar(
        select(MapLayerVersion)
        .where(
            MapLayerVersion.layer == layer,
        )
        .with_for_update()
    )
    if version is None:
        version = MapLayerVersion(layer=layer, geometry_version="", is_ready=False, coverage="")
        session.add(version)
        await session.flush()
    builder = {"roads": build_roads, "population": build_population, "parking": build_parking}[
        layer
    ]
    publication_info = {}
    if layer == "parking":
        features, observations = await build_parking(session, publication_info)
    else:
        features, observations = await builder(session)
    features.sort(key=lambda f: f["id"])
    if len({f["id"] for f in features}) != len(features):
        raise RuntimeError("Duplicate public feature IDs")
    version.geometry_version = hashlib.sha256(
        json.dumps(
            {"selection_rule": 1, "features": features},
            sort_keys=True,
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    version.coverage = "Stored provider coverage only; not all Seoul roads or facilities."
    if layer == "parking":
        version.coverage = (
            "Stored parking records only; not all Seoul facilities. "
            f"Partial runs: {publication_info['partial_runs']}; "
            f"rejected rows: {publication_info['rejected_rows']}. "
            "Prior records omitted by partial collection may be retained with original timestamps."
        )
    await session.execute(delete(MapFeature).where(MapFeature.layer == layer))
    await session.execute(delete(MapObservation).where(MapObservation.layer == layer))
    for f in features:
        session.add(
            MapFeature(
                layer=layer,
                feature_id=f["id"],
                properties=f["properties"],
                is_active=True,
                geometry=func.ST_SetSRID(func.ST_GeomFromGeoJSON(json.dumps(f["geometry"])), 4326),
            )
        )
    session.add_all(observations)
    version.is_ready = True
    await session.flush()
    return len(features)


async def main(layer):
    engine = create_engine(Settings())
    try:
        async with async_sessionmaker(engine).begin() as session:
            count = await publish(session, layer)
            coverage = (await session.get(MapLayerVersion, layer)).coverage
        print(f"Published {layer}: {count} features")
        print(coverage)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("layer", choices=["roads", "population", "parking"])
    asyncio.run(main(parser.parse_args().layer))
