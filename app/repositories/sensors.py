"""Atomic sensor reference updates and independent observation/attempt time guards."""

from datetime import UTC, datetime

from sqlalchemy import func, select

from app.models import PedestrianSensor, PedestrianSensorLatest
from app.modules.data_pipeline.identity import PROCESSOR_VERSION
from app.modules.data_pipeline.normalizers.values import timestamp

SOURCE = "sdot_people"


def instant(value):
    return timestamp(value).value


class SensorWriter:
    def __init__(self, session):
        self.session = session

    def store(self, data, started_at):
        self.session.execute(select(func.pg_advisory_xact_lock(func.hashtext("sdot-storage"))))
        sensors = {
            sensor.sensor_code: sensor
            for sensor in self.session.scalars(
                select(PedestrianSensor)
                .where(
                    PedestrianSensor.source == SOURCE,
                    PedestrianSensor.is_active.is_(True),
                )
                .with_for_update()
            )
        }
        if not sensors or any(
            sensor.source_version != data["reference"]["version"]
            or sensor.boundary_version != data["reference"]["boundary_version"]
            for sensor in sensors.values()
        ):
            raise ValueError("sensor_reference_changed_during_collection")
        if {item["id"] for item in data["items"]} != set(sensors):
            raise ValueError("sensor_reference_changed_during_collection")
        now = datetime.now(UTC)
        results = [
            self.observation(sensors[item["id"]], item, data, started_at, now)
            for item in data["items"]
        ]
        self.session.flush()
        return {
            "observations_written": sum(r["updated"] for r in results),
            "sensors_checked": len(results),
            "sensor_results": results,
        }

    def observation(self, sensor, item, data, started_at, now):
        current = self.session.get(PedestrianSensorLatest, (SOURCE, sensor.sensor_code))
        if current is not None and current.attempt_at > started_at:
            return {
                "sensor_code": sensor.sensor_code,
                "status": "held",
                "reason": "attempt_time_guard",
                "updated": False,
            }
        if current is None:
            current = PedestrianSensorLatest(source=SOURCE, sensor_code=sensor.sensor_code)
            self.session.add(current)
        entries = [data["input_rows"][i] for i in item["input_indexes"]]
        # The earliest received identical fact owns the measurement, not its later repetition.
        evidence = (
            min(
                entries,
                key=lambda r: (
                    instant(r.get("received_at")) or datetime.min.replace(tzinfo=UTC),
                    r["page"],
                    r["row"],
                ),
            )
            if entries
            else None
        )
        receipt = (
            instant(evidence["received_at"])
            if evidence
            else max(
                (instant(p.get("received_at")) for p in data["pages"] if p.get("received_at")),
                default=None,
            )
        )
        observed = instant(item["observed_at"])
        provider_time = instant(item["provider_collected_at"])
        status, reason = item["observation_status"], None
        if data["collection_issues"] and status == "unobserved":
            status, reason = "failed", "collection_incomplete"
        elif status != "valid":
            reason = status
        updated = False
        if status == "valid":
            if not evidence or receipt is None:
                status, reason = "held", "missing_observation_evidence"
            elif current.received_at and receipt < current.received_at:
                status, reason = "held", "observation_receipt_time_guard"
            elif current.observed_at and observed < current.observed_at:
                status, reason = "held", "observation_time_guard"
            elif current.observed_at == observed:
                if current.reference_version != sensor.source_version:
                    status, reason = "held", "observation_reference_changed"
                elif provider_time < current.provider_collected_at:
                    status, reason = "held", "provider_time_guard"
                elif provider_time == current.provider_collected_at and (
                    current.count_10min != item["count_10min"]
                ):
                    status, reason = "conflicting", "same_time_count_conflict"
            # A later provider DATE may correct the same sensor/time. DATA_NO alone cannot.
            if status == "valid" and (
                current.observed_at != observed or provider_time > current.provider_collected_at
            ):
                current.count_10min = item["count_10min"]
                current.observed_at = observed
                current.provider_collected_at = provider_time
                current.received_at = receipt
                current.data_no = item["data_no"]
                current.provider_sensor_id = str(evidence["input"]["SERIAL"])
                model = evidence["input"].get("MODELNAME")
                current.model_name = model if isinstance(model, str) else None
                current.reference_version = sensor.source_version
                current.processed_at = now
                current.processor_version = PROCESSOR_VERSION
                updated = True
        current.attempt_status = status
        current.attempt_reason = reason
        current.attempt_count_10min = item["count_10min"]
        current.attempt_observed_at = observed
        current.attempt_provider_collected_at = provider_time
        current.attempt_received_at = receipt
        current.attempt_data_no = item["data_no"]
        current.attempt_at = started_at
        current.attempt_processed_at = now
        current.attempt_processor_version = PROCESSOR_VERSION
        return {
            "sensor_code": sensor.sensor_code,
            "status": status,
            "reason": reason,
            "updated": updated,
            "has_valid_observation": current.count_10min is not None,
            "matches_current_reference": current.reference_version == sensor.source_version,
        }
