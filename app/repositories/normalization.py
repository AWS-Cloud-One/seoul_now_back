"""Synchronous Core writes, called through AsyncSession.run_sync by worker pipelines.

Only persistence and referential checks live here. Field conversion is pure and
versioned. Revision 0009 contains its own historical conversion rules.
"""

import json
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import and_, func, literal_column, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from app.modules.data_pipeline.identity import PROCESSOR_VERSION
from app.modules.data_pipeline.normalizers.storage_fields import parking_fields


class NormalizedWriter:
    def __init__(self, session, metadata):
        self.session, self.tables = session, metadata.tables

    def t(self, name):
        return self.tables[name]

    def source(self, code, display=None):
        t = self.t("data_sources")
        self.session.execute(
            insert(t)
            .values(
                source_code=code,
                display_name=display or code,
                provider_name="서울특별시"
                if code.startswith("seoul_") or code.startswith("topis")
                else "출처별 수집 자료",
            )
            .on_conflict_do_nothing()
        )

    def upsert(self, name, values, keys, *, newer_payload=False):
        t = self.t(name)
        stmt = insert(t).values(**values)
        changed = {k: getattr(stmt.excluded, k) for k in values if k not in keys}
        if "updated_at" in t.c:
            changed["updated_at"] = func.now()
        where = None
        if newer_payload:
            raw = self.t("raw_payloads")
            # Refer to the ON CONFLICT row; a table column adds an uncorrelated FROM here.
            current_payload = literal_column(f"{t.name}.payload_id", type_=raw.c.payload_id.type)
            old = (
                select(func.coalesce(raw.c.received_at, raw.c.legacy_collected_at, raw.c.stored_at))
                .where(raw.c.payload_id == current_payload)
                .scalar_subquery()
            )
            new = (
                select(func.coalesce(raw.c.received_at, raw.c.legacy_collected_at, raw.c.stored_at))
                .where(raw.c.payload_id == values["payload_id"])
                .scalar_subquery()
            )
            if "observed_at" in values:
                observed = values["observed_at"]
                where = (
                    and_(new >= old, or_(t.c.observed_at.is_(None), t.c.observed_at <= observed))
                    if observed
                    else and_(new >= old, t.c.observed_at.is_(None))
                )
            else:
                where = new >= old
        self.session.execute(
            stmt.on_conflict_do_update(index_elements=keys, set_=changed, where=where)
        )

    def validate_payload(self, source, target, payload_id, *, area_name=None):
        raw, runs = self.t("raw_payloads"), self.t("collection_runs")
        run = self.session.execute(
            select(runs.c.source, runs.c.target_key)
            .join(raw, raw.c.run_id == runs.c.id)
            .where(raw.c.payload_id == payload_id)
        ).first()
        allowed = {target}
        if area_name:
            allowed.add("area_name:" + area_name)
        if run is None or run.source != source or run.target_key not in allowed:
            raise ValueError("payload_source_target_mismatch")

    def exists(self, name, **identity):
        table = self.t(name)
        return bool(
            self.session.scalar(
                select(1)
                .select_from(table)
                .where(*(table.c[key] == value for key, value in identity.items()))
                .limit(1)
            )
        )

    def upsert_geometry(self, name, values, keys):
        self.upsert(
            name,
            {
                **values,
                "geometry": func.ST_Multi(
                    func.ST_SetSRID(func.ST_GeomFromGeoJSON(json.dumps(values["geometry"])), 4326)
                ),
            },
            keys,
        )

    def parking(self, rows, payload_id, received_at, *, complete=False, scope="all"):
        if not rows:
            return dict(accepted=0, held=0)
        source = rows[0]["source"]
        self.validate_payload(source, scope, payload_id)
        if any(row["source"] != source for row in rows):
            raise ValueError("mixed_parking_sources")
        self.source(source)
        records = self.t("parking_source_records")
        facilities = self.t("parking_facilities")
        existing = list(
            self.session.execute(select(records).where(records.c.source == source)).mappings()
        )
        by_hash = {r["legacy_record_id"]: r for r in existing}
        by_code = {r["provider_record_id"] for r in existing}
        accepted = []
        held = 0
        now = datetime.now(UTC)
        for row in rows:
            previous = by_hash.get(row["id"])
            if previous is None and row["source_id"] in by_code:
                held += 1
                continue
            if (
                previous is not None
                and previous["collected_at"]
                and (received_at is None or received_at < previous["collected_at"])
            ):
                accepted.append(previous["source_record_id"])
                continue
            parking_id = previous["parking_id"] if previous else uuid4()
            record_id = previous["source_record_id"] if previous else uuid4()
            facility = (
                self.session.execute(
                    select(facilities).where(facilities.c.parking_id == parking_id)
                )
                .mappings()
                .first()
            )
            if facility is None:
                self.upsert(
                    "parking_facilities",
                    dict(
                        parking_id=parking_id,
                        name=row["name"],
                        location=row["location"],
                        is_active=True,
                    ),
                    ["parking_id"],
                )
            elif facility["identity_status"] == "unverified":
                self.session.execute(
                    update(facilities)
                    .where(facilities.c.parking_id == parking_id)
                    .values(is_active=True, updated_at=now)
                )
            values = dict(
                source_record_id=record_id,
                parking_id=parking_id,
                legacy_record_id=row["id"],
                source=source,
                location=row["location"],
                collected_at=received_at,
                last_payload_id=payload_id,
                is_active=True,
                processed_at=now,
                processor_version=PROCESSOR_VERSION,
                reason_code="SOURCE_TIME_UNKNOWN",
                **parking_fields(row),
            )
            self.upsert("parking_source_records", values, ["source_record_id"])
            accepted.append(record_id)
        # A partial/ambiguous response is not evidence that a facility has closed.
        if complete and not held and scope == "all":
            self.session.execute(
                update(records)
                .where(records.c.source == source, records.c.source_record_id.not_in(accepted))
                .values(is_active=False, updated_at=now)
            )
            active = select(records.c.parking_id).where(records.c.is_active.is_(True))
            self.session.execute(
                update(facilities)
                .where(
                    facilities.c.identity_status == "unverified",
                    facilities.c.parking_id.not_in(active),
                )
                .values(is_active=False, updated_at=now)
            )
        return dict(accepted=len(accepted), held=held)
