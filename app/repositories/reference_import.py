"""Atomic replacement of validated place and sensor reference values. No file access."""

from datetime import date

from sqlalchemy import func, select, update

from app.models import Base, PedestrianSensor, PopulationArea
from app.modules.data_pipeline.identity import PROCESSOR_VERSION
from app.modules.data_pipeline.normalizers.storage_fields import population_area_fields
from app.repositories.normalization import NormalizedWriter


class ReferenceImportWriter:
    def __init__(self, session):
        self.session = session
        self.writer = NormalizedWriter(session, Base.metadata)

    def places(self, features, version, imported_at):
        self.session.execute(select(func.pg_advisory_xact_lock(func.hashtext("place-reference"))))
        current = self.session.scalar(select(func.max(PopulationArea.processed_at)))
        if current and current > imported_at:
            raise ValueError("reference_time_guard")
        if not features or len({f["id"] for f in features}) != len(features):
            raise ValueError("empty_or_duplicate_place_reference")
        self.writer.source("citydata_place_boundaries")
        for feature in features:
            self.writer.upsert_geometry(
                "population_areas",
                dict(
                    **population_area_fields(feature),
                    source="citydata_place_boundaries",
                    source_version=version,
                    is_active=True,
                    processed_at=imported_at,
                    processor_version=PROCESSOR_VERSION,
                ),
                ["area_code"],
            )
        self.session.execute(
            update(PopulationArea)
            .where(
                PopulationArea.is_active.is_(True),
                PopulationArea.area_code.not_in([f["id"] for f in features]),
            )
            .values(is_active=False, processed_at=imported_at, processor_version=PROCESSOR_VERSION)
        )

    def sensors(self, locations, *, version, reference_date, boundary_version, imported_at):
        self.session.execute(select(func.pg_advisory_xact_lock(func.hashtext("sdot-storage"))))
        if not locations or len({item["id"] for item in locations}) != len(locations):
            raise ValueError("empty_or_duplicate_sensor_reference")
        stored = self.session.execute(
            select(
                PedestrianSensor,
                func.ST_X(PedestrianSensor.geometry),
                func.ST_Y(PedestrianSensor.geometry),
            )
            .where(PedestrianSensor.source == "sdot_people")
            .with_for_update()
        ).all()
        incoming_date = date.fromisoformat(reference_date) if reference_date else None
        if any(sensor.reference_checked_at > imported_at for sensor, _, _ in stored):
            raise ValueError("reference_time_guard")
        if any(
            sensor.reference_date
            and sensor.source_version != version
            and (incoming_date is None or incoming_date < sensor.reference_date)
            for sensor, _, _ in stored
        ):
            raise ValueError("unverified_or_older_sensor_reference")
        self.writer.source("sdot_people")
        sensors = {sensor.sensor_code: (sensor, lng, lat) for sensor, lng, lat in stored}
        for item in locations:
            code = item["id"]
            sensor, lng, lat = sensors.get(code, (None, None, None))
            if sensor is None:
                sensor = PedestrianSensor(source="sdot_people", sensor_code=code)
                self.session.add(sensor)
            for key in ("provider_sensor_id", "device_serial", "address"):
                setattr(sensor, key, item[key])
            if (lng, lat) != (item["lng"], item["lat"]):
                sensor.geometry = func.ST_SetSRID(func.ST_MakePoint(item["lng"], item["lat"]), 4326)
            sensor.source_version = version
            sensor.reference_date = incoming_date
            sensor.boundary_version = boundary_version
            sensor.reference_checked_at = imported_at
            sensor.processed_at = imported_at
            sensor.processor_version = PROCESSOR_VERSION
            sensor.is_active = True
        selected = {item["id"] for item in locations}
        for sensor, _, _ in stored:
            if sensor.sensor_code not in selected and sensor.is_active:
                sensor.is_active = False
                sensor.reference_checked_at = imported_at
                sensor.processed_at = imported_at
        self.session.flush()
