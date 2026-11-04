"""Atomic road/administrative references and their current spatial relations."""

import json
from datetime import date

from sqlalchemy import delete, func, select, text, update

from app.models import AdministrativeArea, RoadAreaMapping, RoadLink, RoadLinkMapping
from app.modules.data_pipeline.identity import PROCESSOR_VERSION, fingerprint
from app.modules.data_pipeline.normalizers.storage_fields import road_link_fields

REFERENCE_LOCK = "topis-reference"


def lock_road_reference(session):
    session.execute(select(func.pg_advisory_xact_lock(func.hashtext(REFERENCE_LOCK))))


def rebuild_road_areas(session, link_ids, geometry_version=None):
    if geometry_version is None:
        rows = session.execute(
            select(
                RoadLink.link_id,
                RoadLink.name,
                RoadLink.direction,
                func.ST_AsGeoJSON(RoadLink.geometry),
            )
            .where(
                RoadLink.source == "topis_geometry",
                RoadLink.is_active.is_(True),
                RoadLink.link_id.in_(link_ids),
            )
            .order_by(RoadLink.link_id)
        )
        geometry_version = fingerprint(
            dict(
                selection_rule=2,
                features=[
                    dict(
                        id=link,
                        geometry=json.loads(geo),
                        properties=dict(name=name, link_id=link, direction=direction),
                    )
                    for link, name, direction, geo in rows
                ],
            )
        )
    session.execute(delete(RoadAreaMapping))
    session.execute(
        text("""INSERT INTO road_area_mappings
        (link_id,dong_code,boundary_version,road_geometry_version,processor_version,mapped_at)
        SELECT r.link_id,a.dong_code,a.boundary_version,:version,:processor,now()
        FROM road_links r JOIN administrative_areas a ON ST_Intersects(r.geometry,a.geometry)
        WHERE r.source='topis_geometry' AND r.is_active AND a.is_active
          AND r.link_id = ANY(:links)
          AND ST_Length(ST_Intersection(r.geometry,a.geometry)) > 0"""),
        {"version": geometry_version, "processor": PROCESSOR_VERSION, "links": link_ids},
    )
    session.execute(update(AdministrativeArea).values(road_mapping_version=None))
    session.execute(
        update(AdministrativeArea)
        .where(AdministrativeArea.is_active.is_(True))
        .values(road_mapping_version=geometry_version)
    )
    return geometry_version


def replace_administrative_areas(writer, values, *, complete=False):
    """Keep boundary edits and affected road relations in the same transaction."""
    session = writer.session
    lock_road_reference(session)
    before = {
        row.dong_code: (row.boundary_version, row.is_active, row.processor_version)
        for row in session.execute(
            select(
                AdministrativeArea.dong_code,
                AdministrativeArea.boundary_version,
                AdministrativeArea.is_active,
                AdministrativeArea.processor_version,
            ).where(AdministrativeArea.source == "sgis_areas")
        )
    }
    changed = False
    ids = [value["dong_code"] for value in values]
    for value in values:
        changed |= before.get(value["dong_code"]) != (
            value["boundary_version"],
            True,
            value["processor_version"],
        )
        writer.upsert_geometry("administrative_areas", value, ["dong_code"])
    if complete and ids:
        removed = [key for key, (_, active, _) in before.items() if active and key not in ids]
        if removed:
            session.execute(
                update(AdministrativeArea)
                .where(AdministrativeArea.dong_code.in_(removed))
                .values(is_active=False, road_mapping_version=None)
            )
            changed = True
    # Heal existing stale-version relations on the first refresh after this change, too.
    stale = session.scalar(
        select(RoadAreaMapping.link_id)
        .join(AdministrativeArea)
        .where(
            (RoadAreaMapping.boundary_version != AdministrativeArea.boundary_version)
            | AdministrativeArea.is_active.is_(False)
        )
        .limit(1)
    )
    if changed or stale:
        ids = list(
            session.scalars(
                select(RoadLink.link_id)
                .where(RoadLink.source == "topis_geometry", RoadLink.is_active.is_(True))
                .order_by(RoadLink.link_id)
            )
        )
        rebuild_road_areas(session, ids)


def replace_road_reference(writer, data, imported_at):
    session = writer.session
    lock_road_reference(session)
    current_time = session.scalar(
        select(func.max(RoadLink.processed_at)).where(
            RoadLink.source == "topis_geometry", RoadLink.is_active.is_(True)
        )
    )
    if current_time is not None and imported_at < current_time:
        raise ValueError("reference_time_guard")
    writer.source("topis_geometry")
    writer.source("topis_standard_mapping")
    ref = data["reference"]["inputs"]
    common = dict(processed_at=imported_at, processor_version=PROCESSOR_VERSION)
    ids = [f["id"] for f in data["features"]]
    session.execute(
        update(RoadLink)
        .where(
            RoadLink.source == "topis_geometry",
            RoadLink.is_active.is_(True),
            RoadLink.link_id.not_in(ids),
        )
        .values(is_active=False, processed_at=imported_at)
    )
    for feature in data["features"]:
        writer.upsert_geometry(
            "road_links",
            dict(
                **road_link_fields(feature, ref["vertices"]["version"]),
                source="topis_geometry",
                is_active=True,
                reference_date=date.fromisoformat(ref["vertices"]["reference_date"])
                if ref["vertices"].get("reference_date")
                else None,
                **common,
            ),
            ["link_id"],
        )
    # Only the validated current mapping set is retained.
    session.execute(
        delete(RoadLinkMapping).where(RoadLinkMapping.source == "topis_standard_mapping")
    )
    accepted_ids = set(ids)
    rows = [
        dict(
            **row,
            source="topis_standard_mapping",
            source_version=ref["mapping"]["version"],
            reference_date=date.fromisoformat(ref["mapping"]["reference_date"])
            if ref["mapping"].get("reference_date")
            else None,
            **common,
        )
        for row in data.get("mapping_rows", [])
        if row["service_link_id"] in accepted_ids
    ]
    for start in range(0, len(rows), 1000):
        session.execute(RoadLinkMapping.__table__.insert(), rows[start : start + 1000])
    rebuild_road_areas(session, ids)
    return True
