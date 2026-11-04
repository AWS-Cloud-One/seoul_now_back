"""Atomic sensor reference updates and independent observation/attempt time guards."""

from datetime import UTC, date, datetime
from uuid import UUID

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

    def store(self, data, payload_id, started_at):
        s = self.session
        s.execute(select(func.pg_advisory_xact_lock(func.hashtext("sdot-storage"))))
        stored = list(
            s.execute(
                select(
                    PedestrianSensor,
                    func.ST_X(PedestrianSensor.geometry),
                    func.ST_Y(PedestrianSensor.geometry),
                )
                .where(PedestrianSensor.source == SOURCE)
                .with_for_update()
            )
        )
        sensors = {r.sensor_code: r for r, _, _ in stored}
        positions = {r.sensor_code: (lng, lat) for r, lng, lat in stored}
        ref = data["reference"]
        incoming_date = (
            date.fromisoformat(ref["reference_date"]) if ref.get("reference_date") else None
        )
        previous_dates = [r.reference_date for r in sensors.values() if r.reference_date]
        error = None
        if any(r.reference_checked_at > started_at for r in sensors.values()):
            error = "sensor_reference_time_guard"
        elif (
            previous_dates
            and (incoming_date is None or incoming_date < max(previous_dates))
            and any(r.source_version != ref["sha256"] for r in sensors.values())
        ):
            error = "unverified_or_older_sensor_reference"
        if error:
            return {
                "reference_updated": False,
                "deactivated": 0,
                "sensors_written": 0,
                "observations_written": 0,
                "error_code": error,
                "sensor_results": [],
            }
        now = datetime.now(UTC)
        complete = bool(
            ref.get("complete") and not data["location_quality"]["rejected"] and data["items"]
        )
        incoming_codes = {item["id"] for item in data["items"]}
        deactivated = 0
        if complete:
            for code, sensor in sensors.items():
                if code not in incoming_codes and sensor.is_active:
                    sensor.is_active = False
                    sensor.last_payload_id = payload_id
                    sensor.reference_checked_at = started_at
                    sensor.processed_at = now
                    sensor.processor_version = PROCESSOR_VERSION
                    deactivated += 1
        results = []
        for item in data["items"]:
            code = item["id"]
            sensor = sensors.get(code)
            if sensor is None:
                sensor = PedestrianSensor(source=SOURCE, sensor_code=code)
                s.add(sensor)
            sensor.provider_sensor_id = item["provider_sensor_id"]
            sensor.device_serial = item["device_serial"]
            sensor.address = item["address"]
            if positions.get(code) != (item["lng"], item["lat"]):
                sensor.geometry = func.ST_SetSRID(func.ST_MakePoint(item["lng"], item["lat"]), 4326)
            sensor.source_version = ref["sha256"]
            sensor.reference_date = incoming_date
            sensor.reference_payload_id = UUID(ref["payload_id"])
            sensor.boundary_payload_id = UUID(data["boundary"]["payload_id"])
            sensor.boundary_version = data["boundary"]["version"]
            sensor.last_payload_id = payload_id
            sensor.reference_checked_at = started_at
            sensor.is_active = True
            sensor.processed_at = now
            sensor.processor_version = PROCESSOR_VERSION
            s.flush()
            results.append(self.observation(sensor, item, data, payload_id, started_at, now))
        s.flush()
        return {
            "reference_updated": bool(data["items"]) or bool(deactivated),
            "reference_complete": complete,
            "deactivated": deactivated,
            "sensors_written": len(data["items"]),
            "observations_written": sum(r["updated"] for r in results),
            "error_code": None,
            "sensor_results": results,
        }

    def observation(self, sensor, item, data, payload_id, started_at, now):
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
            if not evidence or not evidence.get("payload_id") or receipt is None:
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
                current.payload_id = UUID(evidence["payload_id"])
                current.reference_version = sensor.source_version
                current.reference_payload_id = sensor.reference_payload_id
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
        current.attempt_payload_id = payload_id
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
