"""Attach national operations/live attempts by a unique, source-local management ID."""

from collections import Counter, defaultdict
from dataclasses import replace
from datetime import UTC, datetime

from sqlalchemy import select, update

from app.modules.data_pipeline.identity import PROCESSOR_VERSION
from app.modules.data_pipeline.normalizers import national_parking as normalizer
from app.modules.data_pipeline.normalizers.parking_attributes import ParkingCountOutOfRange
from app.repositories.parking_state import ParkingStateWriter


class NationalParkingWriter:
    def __init__(self, writer):
        self.writer, self.session = writer, writer.session

    def attach(self, method, candidates, *, blocked=()):
        records = self.writer.t("parking_source_records")
        known = defaultdict(list)
        for old in self.session.execute(
            select(records).where(
                records.c.source == normalizer.SOURCE, records.c.is_active.is_(True)
            )
        ).mappings():
            known[old["provider_record_id"]].append(old)
        reasons, limitations, accepted = Counter(), Counter(), 0
        now = datetime.now(UTC)
        state = ParkingStateWriter(self.writer, now)
        for candidate in candidates:
            code = normalizer.management_id(candidate.record)
            matches = known[code]
            if code in blocked or len(matches) != 1:
                reasons[
                    "conflicting_facility"
                    if code in blocked
                    else "unmatched_management_id"
                    if not matches
                    else "ambiguous_management_id"
                ] += 1
                continue
            old = matches[0]
            record_id = old["source_record_id"]
            if method == "PrkOprInfo":
                if (
                    old["operation_received_at"]
                    and candidate.received_at < old["operation_received_at"]
                ):
                    reasons["older_operation_response"] += 1
                    continue
                try:
                    operation = normalizer.operation(candidate.record)
                except ValueError as exc:
                    reasons[str(exc)] += 1
                    continue
                self.session.execute(
                    update(records)
                    .where(records.c.source_record_id == record_id)
                    .values(
                        **operation["fees"],
                        operation_received_at=candidate.received_at,
                        operation_processed_at=now,
                        operation_processor_version=PROCESSOR_VERSION,
                    )
                )
                state.registration(record_id, replace(candidate, record=operation))
                limitations.update(set(operation["field_issues"].values()))
            elif method == "PrkRealtimeInfo":
                try:
                    live = normalizer.availability(candidate.record, candidate.received_at)
                except ParkingCountOutOfRange as exc:
                    reasons[str(exc)] += 1
                    continue
                state.availability(
                    record_id,
                    replace(
                        candidate,
                        record=dict(
                            source_id=code,
                            availability=live,
                            field_issues={"availability": live["reason_code"]},
                        ),
                    ),
                )
                # Missing provider time is covered by the one-hour receipt policy at read time.
                # Keep SOURCE_TIME_UNKNOWN in the stored evidence, not as a blocking limitation.
                if live["reason_code"] != "SOURCE_TIME_UNKNOWN":
                    limitations[live["reason_code"]] += 1
            else:
                raise ValueError("unsupported_parking_method")
            accepted += 1
        return dict(
            accepted=accepted, rejection_reasons=dict(reasons), limitations=dict(limitations)
        )
