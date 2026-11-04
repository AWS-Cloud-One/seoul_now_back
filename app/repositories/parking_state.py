"""Persist parking hours, field quality and independently ordered live observations."""

from datetime import time

from sqlalchemy import delete, select, update

from app.modules.data_pipeline.identity import PROCESSOR_VERSION

COUNTS = (
    "total_spaces",
    "occupied_spaces",
    "reported_available_spaces",
    "calculated_available_spaces",
)


class ParkingStateWriter:
    def __init__(self, writer, now):
        self.writer, self.session, self.now = writer, writer.session, now

    def issues(self, record_id, issues, *, live=False, section=None):
        table = self.writer.t("parking_field_issues")

        def belongs(path):
            if section == "operation":
                return path.startswith(("fees.", "operating_hours."))
            if section == "registration":
                return not path.startswith(("availability", "fees.", "operating_hours."))
            return path.startswith("availability") == live

        operation = table.c.field_path.startswith("fees.") | table.c.field_path.startswith(
            "operating_hours."
        )
        availability = table.c.field_path.startswith("availability")
        condition = (
            operation
            if section == "operation"
            else ~operation & ~availability
            if section == "registration"
            else availability
            if live
            else ~availability
        )
        self.session.execute(delete(table).where(table.c.source_record_id == record_id, condition))
        for path, reason in issues.items():
            if belongs(path):
                self.session.execute(
                    table.insert().values(
                        source_record_id=record_id,
                        field_path=path,
                        reason_code=reason,
                        processed_at=self.now,
                        processor_version=PROCESSOR_VERSION,
                    )
                )

    def registration(self, record_id, candidate):
        if candidate.record["operating_hours"] is None:
            self.issues(
                record_id,
                candidate.record["field_issues"],
                section="registration",
            )
            return
        table = self.writer.t("parking_operating_hours")
        self.session.execute(delete(table).where(table.c.source_record_id == record_id))
        for period in candidate.record["operating_hours"]:
            self.session.execute(
                table.insert().values(
                    source_record_id=record_id,
                    day_type=period["day_type"],
                    period_no=period.get("period_no", 1),
                    opens_at=time.fromisoformat(period["opens_at"]) if period["opens_at"] else None,
                    closes_at=time.fromisoformat(period["closes_at"])
                    if period["closes_at"]
                    else None,
                    end_day_offset=period["end_day_offset"],
                    state=period["state"],
                    reason_code=period["reason_code"],
                    processed_at=self.now,
                    processor_version=PROCESSOR_VERSION,
                )
            )
        issues = dict(candidate.record["field_issues"])
        for period in candidate.record["operating_hours"]:
            for field, reason in period["field_issues"].items():
                issues[f"operating_hours.{period['day_type']}.{field}"] = reason
        self.issues(record_id, issues, section=candidate.record.get("issue_section"))

    def availability(self, record_id, candidate, *, ambiguous_scope=False, conflict=None):
        live = candidate.record["availability"]
        if live is None:
            return
        table = self.writer.t("parking_availability_latest")
        old = (
            self.session.execute(
                select(table).where(table.c.source_record_id == record_id).with_for_update()
            )
            .mappings()
            .first()
        )
        reason, status = live["reason_code"], "accepted"
        if ambiguous_scope:
            reason, status = "AMBIGUOUS_AVAILABILITY_SCOPE", "scope_unverified"
        elif conflict:
            reason, status = conflict, "conflict"
        elif live["reason_code"] == "SOURCE_TIME_UNKNOWN":
            status = "unknown"
        elif not live["usable"]:
            status = {"not_provided": "not_provided", "unknown": "unknown"}.get(
                live["provision_state"], "invalid"
            )
        elif old and old["observed_at"]:
            if live["observed_at"] < old["observed_at"]:
                reason, status = "OLDER_OBSERVATION", "stale"
            elif live["observed_at"] == old["observed_at"] and (
                any(live[key] != old[key] for key in (*COUNTS, "method"))
                or old["reason_code"] == "SAME_TIME_CONFLICT"
            ):
                reason, status = "SAME_TIME_CONFLICT", "conflict"

        changes = {}
        if status == "accepted" and (
            not old or old["observed_at"] is None or live["observed_at"] > old["observed_at"]
        ):
            changes.update(
                {key: live[key] for key in (*COUNTS, "observed_at", "received_at", "method")},
                usable=True,
                reason_code=None,
                processed_at=self.now,
                processor_version=PROCESSOR_VERSION,
            )
        elif old and (
            ambiguous_scope
            or reason == "SAME_TIME_CONFLICT"
            or (
                conflict == "CONFLICTING_PARKING_OBSERVATION"
                and old["observed_at"] is not None
                and live["observed_at"] == old["observed_at"]
            )
        ):
            # Retain the original numbers and provenance, but do not claim they are reliable.
            changes.update(
                usable=False,
                reason_code=(
                    "AMBIGUOUS_AVAILABILITY_SCOPE" if ambiguous_scope else "SAME_TIME_CONFLICT"
                ),
            )
        latest_attempt = (
            not old
            or old["attempt_received_at"] is None
            or (
                candidate.received_at is not None
                and candidate.received_at >= old["attempt_received_at"]
            )
        )
        if latest_attempt:
            if old and old["observed_at"] is None and "observed_at" not in changes:
                changes["reason_code"] = reason
            changes.update(
                **{"attempt_" + key: live[key] for key in COUNTS},
                attempt_received_at=candidate.received_at,
                attempt_observed_at=live["observed_at"],
                attempt_provision_code=live["provision_code"],
                attempt_provision_name=live["provision_name"],
                attempt_provision_state=live["provision_state"],
                attempt_status=status,
                attempt_reason_code=reason,
                attempt_processed_at=self.now,
                attempt_processor_version=PROCESSOR_VERSION,
            )
            issues = dict(candidate.record["field_issues"])
            if reason:
                issues["availability"] = reason
            self.issues(record_id, issues, live=True)
        if not old:
            self.session.execute(
                table.insert().values(
                    source_record_id=record_id,
                    scope=live["scope"],
                    scope_key=candidate.record["source_id"],
                    **(
                        {"method": live["method"], "usable": False, "reason_code": reason} | changes
                    ),
                )
            )
        elif changes:
            self.session.execute(
                update(table).where(table.c.source_record_id == record_id).values(**changes)
            )

    def invalidate_scope(self, record_ids):
        """A newly discovered second physical space also invalidates a prior one-space reading."""
        if not record_ids:
            return
        table = self.writer.t("parking_availability_latest")
        self.session.execute(
            update(table)
            .where(table.c.source_record_id.in_(record_ids))
            .values(usable=False, reason_code="AMBIGUOUS_AVAILABILITY_SCOPE")
        )
