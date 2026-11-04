"""Finalize parking, road mapping and sensor storage in one transition.

Revision ID: 0010
Revises: 0009

The previously applied 0009 is unchanged. DDL is fixed here; future runtime
models/converters cannot change this upgrade. Legacy hours are retained as
unverified text, and no observations are invented from installation data.
"""

import sqlalchemy as sa
from alembic import context, op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

SCHEMA_SQL = """
ALTER TABLE parking_source_records ADD COLUMN daily_flat_fee_krw NUMERIC;

ALTER TABLE parking_source_records ADD COLUMN operation_payload_id UUID;

ALTER TABLE parking_source_records ADD COLUMN operation_received_at TIMESTAMP WITH TIME ZONE;

ALTER TABLE parking_source_records ADD COLUMN operation_processed_at TIMESTAMP WITH TIME ZONE;

ALTER TABLE parking_source_records ADD COLUMN operation_processor_version TEXT;

ALTER TABLE parking_source_records ADD COLUMN management_type_code TEXT;

ALTER TABLE parking_source_records ADD COLUMN management_type_name TEXT;

ALTER TABLE parking_source_records ADD COLUMN base_usable BOOLEAN DEFAULT 'false' NOT NULL;

ALTER TABLE parking_source_records ADD COLUMN additional_usable BOOLEAN DEFAULT 'false' NOT
    NULL;

ALTER TABLE parking_source_records ADD COLUMN parking_type_code TEXT;

ALTER TABLE parking_source_records ADD COLUMN parking_type_name TEXT;

ALTER TABLE parking_source_records ADD COLUMN operation_code TEXT;

ALTER TABLE parking_source_records ADD COLUMN operation_name TEXT;

ALTER TABLE parking_source_records ADD COLUMN phone TEXT;

ALTER TABLE parking_source_records ADD COLUMN payment_code TEXT;

ALTER TABLE parking_source_records ADD COLUMN payment_name TEXT;

ALTER TABLE parking_source_records ADD COLUMN is_paid BOOLEAN;

ALTER TABLE parking_source_records ADD COLUMN night_free_open_code TEXT;

ALTER TABLE parking_source_records ADD COLUMN night_free_open_name TEXT;

ALTER TABLE parking_source_records ADD COLUMN night_free_open BOOLEAN;

ALTER TABLE parking_source_records ADD COLUMN saturday_payment_code TEXT;

ALTER TABLE parking_source_records ADD COLUMN saturday_payment_name TEXT;

ALTER TABLE parking_source_records ADD COLUMN saturday_is_paid BOOLEAN;

ALTER TABLE parking_source_records ADD COLUMN holiday_payment_code TEXT;

ALTER TABLE parking_source_records ADD COLUMN holiday_payment_name TEXT;

ALTER TABLE parking_source_records ADD COLUMN holiday_is_paid BOOLEAN;

ALTER TABLE parking_source_records ADD COLUMN management_group_id TEXT;

ALTER TABLE parking_source_records ADD COLUMN realtime_link_code TEXT;

ALTER TABLE parking_source_records ADD COLUMN realtime_link_name TEXT;

ALTER TABLE parking_source_records ADD COLUMN sharing_code TEXT;

ALTER TABLE parking_source_records ADD COLUMN sharing_notes TEXT;

ALTER TABLE parking_source_records ADD COLUMN is_shared BOOLEAN;

ALTER TABLE parking_source_records ADD CONSTRAINT
    ck_parking_source_records_daily_flat_fee_krw_finite CHECK (daily_flat_fee_krw >= 0 AND
    daily_flat_fee_krw < 'Infinity'::numeric);

ALTER TABLE parking_source_records ADD CONSTRAINT
    fk_parking_source_records_operation_payload_id_raw_payloads FOREIGN
    KEY(operation_payload_id) REFERENCES raw_payloads (payload_id);

ALTER TABLE road_link_mappings ADD COLUMN sequence_status TEXT DEFAULT 'unknown' NOT NULL;

ALTER TABLE road_link_mappings ADD CONSTRAINT ck_road_link_mappings_sequence_status CHECK
    (sequence_status IN ('known','unknown','ambiguous'));

CREATE TABLE pedestrian_sensors (
        source TEXT NOT NULL,
        sensor_code TEXT NOT NULL,
        provider_sensor_id TEXT NOT NULL,
        device_serial TEXT NOT NULL,
        address TEXT NOT NULL,
        geometry geometry(POINT,4326) NOT NULL,
        source_version TEXT NOT NULL,
        reference_date DATE,
        reference_payload_id UUID NOT NULL,
        boundary_payload_id UUID NOT NULL,
        boundary_version TEXT NOT NULL,
        last_payload_id UUID NOT NULL,
        reference_checked_at TIMESTAMP WITH TIME ZONE NOT NULL,
        is_active BOOLEAN DEFAULT 'true' NOT NULL,
        processed_at TIMESTAMP WITH TIME ZONE NOT NULL,
        processor_version TEXT NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        CONSTRAINT pk_pedestrian_sensors PRIMARY KEY (source, sensor_code),
        CONSTRAINT ck_pedestrian_sensors_source_nonblank CHECK (btrim(source) <> ''),
        CONSTRAINT ck_pedestrian_sensors_sensor_code_nonblank CHECK (btrim(sensor_code) <> ''),
        CONSTRAINT ck_pedestrian_sensors_provider_sensor_id_nonblank CHECK
        (btrim(provider_sensor_id) <> ''),
        CONSTRAINT ck_pedestrian_sensors_device_serial_nonblank CHECK (btrim(device_serial) <>
        ''),
        CONSTRAINT ck_pedestrian_sensors_address_nonblank CHECK (btrim(address) <> ''),
        CONSTRAINT ck_pedestrian_sensors_source_version_nonblank CHECK (btrim(source_version) <>
        ''),
        CONSTRAINT ck_pedestrian_sensors_boundary_version_nonblank CHECK
        (btrim(boundary_version) <> ''),
        CONSTRAINT ck_pedestrian_sensors_processor_version_nonblank CHECK
        (btrim(processor_version) <> ''),
        CONSTRAINT ck_pedestrian_sensors_sensor_code_format CHECK (sensor_code ~
        '^[1-9][0-9]*$'),
        CONSTRAINT ck_pedestrian_sensors_valid_point CHECK (NOT ST_IsEmpty(geometry) AND
        ST_IsValid(geometry) AND ST_X(geometry) BETWEEN -180 AND 180 AND ST_Y(geometry) BETWEEN
        -90 AND 90),
        CONSTRAINT fk_pedestrian_sensors_source_data_sources FOREIGN KEY(source) REFERENCES
        data_sources (source_code),
        CONSTRAINT fk_pedestrian_sensors_reference_payload_id_raw_payloads FOREIGN
        KEY(reference_payload_id) REFERENCES raw_payloads (payload_id) ON DELETE RESTRICT,
        CONSTRAINT fk_pedestrian_sensors_boundary_payload_id_raw_payloads FOREIGN
        KEY(boundary_payload_id) REFERENCES raw_payloads (payload_id) ON DELETE RESTRICT,
        CONSTRAINT fk_pedestrian_sensors_last_payload_id_raw_payloads FOREIGN
        KEY(last_payload_id) REFERENCES raw_payloads (payload_id) ON DELETE RESTRICT
);

CREATE INDEX idx_pedestrian_sensors_geometry ON pedestrian_sensors USING gist (geometry);

CREATE TABLE parking_availability_latest (
        source_record_id UUID NOT NULL,
        scope TEXT NOT NULL,
        scope_key TEXT NOT NULL,
        method TEXT NOT NULL,
        total_spaces INTEGER,
        occupied_spaces INTEGER,
        reported_available_spaces INTEGER,
        calculated_available_spaces INTEGER,
        observed_at TIMESTAMP WITH TIME ZONE,
        received_at TIMESTAMP WITH TIME ZONE,
        payload_id UUID,
        processed_at TIMESTAMP WITH TIME ZONE,
        processor_version TEXT,
        usable BOOLEAN DEFAULT 'false' NOT NULL,
        reason_code TEXT,
        attempt_payload_id UUID NOT NULL,
        attempt_received_at TIMESTAMP WITH TIME ZONE,
        attempt_observed_at TIMESTAMP WITH TIME ZONE,
        attempt_provision_code TEXT,
        attempt_provision_name TEXT,
        attempt_provision_state TEXT NOT NULL,
        attempt_total_spaces INTEGER,
        attempt_occupied_spaces INTEGER,
        attempt_reported_available_spaces INTEGER,
        attempt_calculated_available_spaces INTEGER,
        attempt_status TEXT NOT NULL,
        attempt_reason_code TEXT,
        attempt_processed_at TIMESTAMP WITH TIME ZONE NOT NULL,
        attempt_processor_version TEXT NOT NULL,
        CONSTRAINT pk_parking_availability_latest PRIMARY KEY (source_record_id),
        CONSTRAINT ck_parking_availability_latest_scope CHECK (scope = 'provider_record'),
        CONSTRAINT ck_parking_availability_latest_method CHECK (method IN
        ('reported','calculated')),
        CONSTRAINT ck_parking_availability_latest_capacity CHECK (total_spaces >= 0),
        CONSTRAINT ck_parking_availability_latest_occupied CHECK (occupied_spaces BETWEEN 0 AND
        total_spaces),
        CONSTRAINT ck_parking_availability_latest_reported CHECK (reported_available_spaces
        BETWEEN 0 AND total_spaces),
        CONSTRAINT ck_parking_availability_latest_calculated CHECK (calculated_available_spaces
        BETWEEN 0 AND total_spaces),
        CONSTRAINT ck_parking_availability_latest_observation CHECK (payload_id IS NULL OR
        (observed_at IS NOT NULL AND received_at IS NOT NULL AND total_spaces IS NOT NULL AND
        processed_at IS NOT NULL AND processor_version IS NOT NULL AND observed_at <=
        received_at AND ((method = 'reported' AND reported_available_spaces IS NOT NULL AND
        occupied_spaces IS NULL AND calculated_available_spaces IS NULL) OR (method =
        'calculated' AND occupied_spaces IS NOT NULL AND calculated_available_spaces IS NOT NULL
        AND reported_available_spaces IS NULL AND calculated_available_spaces = total_spaces -
        occupied_spaces)))),
        CONSTRAINT ck_parking_availability_latest_usable CHECK (NOT usable OR payload_id IS NOT
        NULL),
        CONSTRAINT ck_parking_availability_latest_provision CHECK (attempt_provision_state IN
        ('provided','not_provided','unknown')),
        CONSTRAINT ck_parking_availability_latest_attempt_status CHECK (attempt_status IN
        ('accepted','not_provided','unknown','invalid','stale','conflict','scope_unverified')),
        CONSTRAINT fk_parking_availability_latest_source_record_id_parking_7211 FOREIGN
        KEY(source_record_id) REFERENCES parking_source_records (source_record_id) ON DELETE
        CASCADE,
        CONSTRAINT fk_parking_availability_latest_payload_id_raw_payloads FOREIGN
        KEY(payload_id) REFERENCES raw_payloads (payload_id),
        CONSTRAINT fk_parking_availability_latest_attempt_payload_id_raw_payloads FOREIGN
        KEY(attempt_payload_id) REFERENCES raw_payloads (payload_id)
);

CREATE TABLE parking_field_issues (
        source_record_id UUID NOT NULL,
        field_path TEXT NOT NULL,
        reason_code TEXT NOT NULL,
        payload_id UUID NOT NULL,
        processed_at TIMESTAMP WITH TIME ZONE NOT NULL,
        processor_version TEXT NOT NULL,
        CONSTRAINT pk_parking_field_issues PRIMARY KEY (source_record_id, field_path),
        CONSTRAINT fk_parking_field_issues_source_record_id_parking_source_records FOREIGN
        KEY(source_record_id) REFERENCES parking_source_records (source_record_id) ON DELETE
        CASCADE,
        CONSTRAINT fk_parking_field_issues_payload_id_raw_payloads FOREIGN KEY(payload_id)
        REFERENCES raw_payloads (payload_id)
);

CREATE TABLE parking_operating_hours (
        source_record_id UUID NOT NULL,
        day_type TEXT NOT NULL,
        period_no INTEGER DEFAULT '1' NOT NULL,
        opens_at TIME WITHOUT TIME ZONE,
        closes_at TIME WITHOUT TIME ZONE,
        end_day_offset INTEGER,
        state TEXT NOT NULL,
        reason_code TEXT,
        source_text TEXT,
        payload_id UUID,
        processed_at TIMESTAMP WITH TIME ZONE,
        processor_version TEXT,
        CONSTRAINT pk_parking_operating_hours PRIMARY KEY (source_record_id, day_type,
        period_no),
        CONSTRAINT ck_parking_operating_hours_day_type CHECK (day_type IN
        ('weekday','weekend','holiday','monday','tuesday','wednesday','thursday','friday','saturday','sunday','sunday_holiday')),
        CONSTRAINT ck_parking_operating_hours_period_no CHECK (period_no >= 1),
        CONSTRAINT ck_parking_operating_hours_state CHECK (state IN
        ('valid','missing','invalid','unknown')),
        CONSTRAINT ck_parking_operating_hours_end_day_offset CHECK (end_day_offset IN (0,1)),
        CONSTRAINT ck_parking_operating_hours_valid_interval CHECK (state <> 'valid' OR
        (opens_at IS NOT NULL AND closes_at IS NOT NULL AND end_day_offset IS NOT NULL AND
        (end_day_offset = 1 OR closes_at > opens_at))),
        CONSTRAINT ck_parking_operating_hours_processor_version_nonblank CHECK
        (btrim(processor_version) <> ''),
        CONSTRAINT ck_parking_operating_hours_processing_pair CHECK ((processor_version IS NULL)
        = (processed_at IS NULL)),
        CONSTRAINT fk_parking_operating_hours_source_record_id_parking_sou_a253 FOREIGN
        KEY(source_record_id) REFERENCES parking_source_records (source_record_id) ON DELETE
        CASCADE,
        CONSTRAINT fk_parking_operating_hours_payload_id_raw_payloads FOREIGN KEY(payload_id)
        REFERENCES raw_payloads (payload_id)
);

CREATE TABLE pedestrian_sensor_latest (
        source TEXT NOT NULL,
        sensor_code TEXT NOT NULL,
        count_10min BIGINT,
        observed_at TIMESTAMP WITH TIME ZONE,
        provider_collected_at TIMESTAMP WITH TIME ZONE,
        received_at TIMESTAMP WITH TIME ZONE,
        data_no TEXT,
        provider_sensor_id TEXT,
        model_name TEXT,
        payload_id UUID,
        reference_version TEXT,
        reference_payload_id UUID,
        processed_at TIMESTAMP WITH TIME ZONE,
        processor_version TEXT,
        attempt_status TEXT NOT NULL,
        attempt_reason TEXT,
        attempt_count_10min BIGINT,
        attempt_observed_at TIMESTAMP WITH TIME ZONE,
        attempt_provider_collected_at TIMESTAMP WITH TIME ZONE,
        attempt_received_at TIMESTAMP WITH TIME ZONE,
        attempt_data_no TEXT,
        attempt_payload_id UUID NOT NULL,
        attempt_at TIMESTAMP WITH TIME ZONE NOT NULL,
        attempt_processed_at TIMESTAMP WITH TIME ZONE NOT NULL,
        attempt_processor_version TEXT NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        CONSTRAINT pk_pedestrian_sensor_latest PRIMARY KEY (source, sensor_code),
        CONSTRAINT fk_pedestrian_sensor_latest_source_pedestrian_sensors FOREIGN KEY(source,
        sensor_code) REFERENCES pedestrian_sensors (source, sensor_code) ON DELETE RESTRICT,
        CONSTRAINT ck_pedestrian_sensor_latest_data_no_nonblank CHECK (btrim(data_no) <> ''),
        CONSTRAINT ck_pedestrian_sensor_latest_provider_sensor_id_nonblank CHECK
        (btrim(provider_sensor_id) <> ''),
        CONSTRAINT ck_pedestrian_sensor_latest_reference_version_nonblank CHECK
        (btrim(reference_version) <> ''),
        CONSTRAINT ck_pedestrian_sensor_latest_processor_version_nonblank CHECK
        (btrim(processor_version) <> ''),
        CONSTRAINT ck_pedestrian_sensor_latest_attempt_processor_version_nonblank CHECK
        (btrim(attempt_processor_version) <> ''),
        CONSTRAINT ck_pedestrian_sensor_latest_nonnegative_counts CHECK (count_10min >= 0 AND
        attempt_count_10min >= 0),
        CONSTRAINT ck_pedestrian_sensor_latest_attempt_status CHECK (attempt_status IN
        ('valid','partial','unobserved','invalid','conflicting','ambiguous_data_no','failed','held')),
        CONSTRAINT ck_pedestrian_sensor_latest_observation_provenance CHECK
        (num_nonnulls(count_10min, observed_at, payload_id, data_no, received_at,
        provider_collected_at, reference_version, reference_payload_id, processed_at,
        processor_version, provider_sensor_id) IN (0,11)),
        CONSTRAINT ck_pedestrian_sensor_latest_observation_times CHECK (observed_at <=
        provider_collected_at AND provider_collected_at <= received_at),
        CONSTRAINT fk_pedestrian_sensor_latest_payload_id_raw_payloads FOREIGN KEY(payload_id)
        REFERENCES raw_payloads (payload_id) ON DELETE RESTRICT,
        CONSTRAINT fk_pedestrian_sensor_latest_reference_payload_id_raw_payloads FOREIGN
        KEY(reference_payload_id) REFERENCES raw_payloads (payload_id) ON DELETE RESTRICT,
        CONSTRAINT fk_pedestrian_sensor_latest_attempt_payload_id_raw_payloads FOREIGN
        KEY(attempt_payload_id) REFERENCES raw_payloads (payload_id) ON DELETE RESTRICT
);

"""

OLD_HOURS = {
    "weekday_hours": "weekday",
    "saturday_hours": "saturday",
    "sunday_holiday_hours": "sunday_holiday",
}


def upgrade():
    if context.is_offline_mode():
        raise RuntimeError("0010 requires online preservation checks before dropping old columns")
    connection = op.get_bind()
    connection.exec_driver_sql("SET LOCAL lock_timeout = '5s'")
    for statement in SCHEMA_SQL.split(";"):
        if statement.strip():
            connection.exec_driver_sql(statement)
    connection.exec_driver_sql("""
        UPDATE parking_source_records SET
            base_usable = coalesce(base_minutes > 0 AND base_fee_krw IS NOT NULL, false),
            additional_usable = coalesce(
                additional_minutes > 0 AND additional_fee_krw IS NOT NULL, false)
    """)
    for column, day in OLD_HOURS.items():
        expected = connection.exec_driver_sql(
            f"SELECT count(*) FROM parking_source_records WHERE {column} IS NOT NULL"
        ).scalar_one()
        connection.execute(
            sa.text(f"""
            INSERT INTO parking_operating_hours
                (source_record_id, day_type, period_no, state, reason_code,
                 source_text, payload_id, processed_at, processor_version)
            SELECT source_record_id, :day, 1, 'unknown', 'LEGACY_HOURS_UNVERIFIED',
                {column}, last_payload_id, processed_at, processor_version
            FROM parking_source_records WHERE {column} IS NOT NULL
        """),
            {"day": day},
        )
        preserved = connection.execute(
            sa.text(f"""
            SELECT count(*) FROM parking_source_records r
            JOIN parking_operating_hours h ON h.source_record_id=r.source_record_id
                AND h.day_type=:day AND h.period_no=1
            WHERE r.{column} IS NOT NULL AND h.source_text IS NOT DISTINCT FROM r.{column}
                AND h.payload_id IS NOT DISTINCT FROM r.last_payload_id
                AND h.processed_at IS NOT DISTINCT FROM r.processed_at
                AND h.processor_version IS NOT DISTINCT FROM r.processor_version
        """),
            {"day": day},
        ).scalar_one()
        if preserved != expected:
            raise RuntimeError("0010 legacy operating-hours preservation mismatch")
        connection.exec_driver_sql(f"ALTER TABLE parking_source_records DROP COLUMN {column}")


def downgrade():
    raise RuntimeError(
        "0010 downgrade requires restoring the DB and evidence-file backup together; "
        "new observations and operating-hour rows must not be silently discarded"
    )
