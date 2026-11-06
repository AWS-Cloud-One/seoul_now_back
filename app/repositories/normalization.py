"""Synchronous Core writes, called through AsyncSession.run_sync by worker pipelines.

Only persistence and referential checks live here. Field conversion is pure and
versioned. Revision 0009 contains its own historical conversion rules.
"""

import json
from collections import defaultdict
from datetime import UTC, datetime
from uuid import uuid4

from geoalchemy2 import Geometry
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from app.modules.data_pipeline.identity import PROCESSOR_VERSION
from app.modules.data_pipeline.normalizers.parking_resolution import resolve_parking_identities
from app.modules.data_pipeline.normalizers.storage_fields import parking_fields
from app.repositories.parking_state import ParkingStateWriter


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

    def upsert(self, name, values, keys, *, return_changed=False, preserve=()):
        t = self.t(name)
        stmt = insert(t).values(**values)
        changed = {
            k: getattr(stmt.excluded, k) for k in values if k not in keys and k not in preserve
        }
        if "updated_at" in t.c:
            changed["updated_at"] = func.now()
        stmt = stmt.on_conflict_do_update(index_elements=keys, set_=changed)
        if return_changed:
            return self.session.scalar(stmt.returning(t.c[keys[0]])) is not None
        self.session.execute(stmt)
        return None

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
        table = self.t(name)
        geometry = func.ST_Multi(
            func.ST_SetSRID(func.ST_GeomFromGeoJSON(json.dumps(values["geometry"])), 4326)
        )
        identity = [table.c[key] == values[key] for key in keys]
        # Preserve coordinate ordering (road direction/ring representation), not just coverage.
        # Lock only writers so comparison and update cannot race; snapshot readers never wait.
        same = self.session.scalar(
            select(func.ST_OrderingEquals(table.c.geometry, geometry))
            .where(*identity)
            .with_for_update()
        )
        if same:
            self.session.execute(
                update(table)
                .where(*identity)
                .values(**{k: v for k, v in values.items() if k not in {*keys, "geometry"}})
            )
            return
        self.upsert(
            name,
            {**values, "geometry": geometry},
            keys,
        )

    def parking(self, candidates, *, complete=False, scope="all", conflicts=()):
        all_candidates = [*candidates, *(item[0] for item in conflicts)]
        if not all_candidates:
            return dict(accepted=0, held=0, rejection_reasons={}, held_identity_keys=[])
        rows = [candidate.record for candidate in candidates]
        source = all_candidates[0].record["source"]
        if any(c.record["source"] != source for c in all_candidates):
            raise ValueError("mixed_parking_sources")
        inputs = {candidate.record["identity_key"]: candidate for candidate in candidates}
        if len(inputs) != len(candidates):
            raise ValueError("unresolved_parking_duplicates")
        self.source(source)
        records = self.t("parking_source_records")
        facilities = self.t("parking_facilities")
        points = self.session.execute(
            select(
                records,
                func.ST_X(records.c.location.cast(Geometry)).label("longitude"),
                func.ST_Y(records.c.location.cast(Geometry)).label("latitude"),
            )
            .where(records.c.source == source)
            .with_for_update()
        ).mappings()
        existing = [dict(old, position=(old["longitude"], old["latitude"])) for old in points]
        resolved, reasons = resolve_parking_identities(rows, existing)
        owners = dict(
            self.session.execute(
                select(records.c.parking_id, func.count()).group_by(records.c.parking_id)
            ).all()
        )
        accepted = []
        held = sum(reasons.values())
        now = datetime.now(UTC)
        state = ParkingStateWriter(self, now)
        spaces = defaultdict(set)
        for old in existing:
            spaces[old["provider_record_id"]].add(old["source_record_id"])
        for row, previous in resolved:
            spaces[row["source_id"]].add(
                previous["source_record_id"] if previous else row["identity_key"]
            )
        ambiguous_codes = {code for code, ids in spaces.items() if len(ids) > 1}
        state.invalidate_scope(
            [
                old["source_record_id"]
                for old in existing
                if old["provider_record_id"] in ambiguous_codes
            ]
        )
        for row, previous in resolved:
            evidence = inputs[row["identity_key"]]
            received_at = evidence.received_at
            static_is_current = not (
                previous is not None
                and previous["collected_at"]
                and (received_at is None or received_at < previous["collected_at"])
            )
            parking_id = previous["parking_id"] if previous else uuid4()
            record_id = previous["source_record_id"] if previous else uuid4()
            if static_is_current:
                facility = (
                    self.session.execute(
                        select(
                            facilities,
                            func.ST_X(facilities.c.location.cast(Geometry)).label("longitude"),
                            func.ST_Y(facilities.c.location.cast(Geometry)).label("latitude"),
                        )
                        .where(facilities.c.parking_id == parking_id)
                        .with_for_update()
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
                    representative = {}
                    if not facility["is_active"]:
                        representative["is_active"] = True
                    if owners.get(parking_id) == 1:
                        if facility["name"] != row["name"]:
                            representative["name"] = row["name"]
                        if (facility["longitude"], facility["latitude"]) != row["position"]:
                            representative["location"] = row["location"]
                    if representative:
                        self.session.execute(
                            update(facilities)
                            .where(facilities.c.parking_id == parking_id)
                            .values(**representative, updated_at=now)
                        )
                values = dict(
                    source_record_id=record_id,
                    parking_id=parking_id,
                    legacy_record_id=(previous["legacy_record_id"] if previous else None)
                    or row["identity_key"],
                    source=source,
                    location=row["location"],
                    collected_at=received_at,
                    is_active=True,
                    processed_at=now,
                    processor_version=PROCESSOR_VERSION,
                    **parking_fields(row),
                )
                if row["fees"] is not None:
                    values.update(
                        operation_received_at=received_at,
                        operation_processed_at=now,
                        operation_processor_version=PROCESSOR_VERSION,
                    )
                self.upsert(
                    "parking_source_records",
                    values,
                    ["source_record_id"],
                    preserve=("location",)
                    if previous and previous["position"] == row["position"]
                    else (),
                )
                state.registration(record_id, evidence)
            state.availability(
                record_id,
                evidence.observation or evidence,
                ambiguous_scope=row["source_id"] in ambiguous_codes,
            )
            accepted.append(record_id)
        # Conflicting candidates cannot change registration. Record their attempt only
        # when an existing identity is unambiguous; otherwise record only its rejection count.
        for candidate, reason in sorted(
            conflicts,
            key=lambda item: (
                item[0].received_at or datetime.min.replace(tzinfo=UTC),
                item[0].origin,
                item[0].row_index if item[0].row_index is not None else -1,
            ),
        ):
            matched, _ = resolve_parking_identities([candidate.record], existing)
            if matched and matched[0][1] is not None:
                old = matched[0][1]
                state.availability(
                    old["source_record_id"],
                    candidate,
                    conflict=reason.upper(),
                    ambiguous_scope=candidate.record["source_id"] in ambiguous_codes,
                )
        # A partial/ambiguous response is not evidence that a facility has closed.
        if complete and not held and not conflicts and scope == "all":
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
        return dict(
            accepted=len(accepted),
            held=held,
            rejection_reasons=dict(reasons),
            held_identity_keys=sorted(set(inputs) - {row["identity_key"] for row, _ in resolved}),
        )
