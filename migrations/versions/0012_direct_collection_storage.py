"""Replace raw evidence references with typed current state.

Revision ID: 0012
Revises: 0011
Only actual receipt timestamps are copied; unknown receipts remain NULL.
National parking safely restarts its current scan, preserving existing business rows.
Runtime readers and writers use typed state only; old response tables are removed.
"""

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade():
    # Preserve per-road attempts created by older/manual collectors after 0011.
    # Runtime retention must not need the removed response tables for this conversion.
    op.execute("""
        INSERT INTO road_traffic_attempt_latest
            (link_id,run_id,attempted_at,status,error_code,attempts,observation_updated)
        SELECT DISTINCT ON (l.link_id)
            l.link_id,r.id,COALESCE(r.finished_at,r.started_at),r.status,r.error_code,1,
            COALESCE(p.run_id=r.id,false)
        FROM collection_runs r JOIN road_links l ON r.target_key='link:' || l.link_id
        LEFT JOIN road_traffic_latest t ON t.link_id=l.link_id AND t.source='topis_traffic'
        LEFT JOIN raw_payloads p ON p.payload_id=t.payload_id
        WHERE r.source='topis_traffic' AND r.status <> 'running'
            AND r.run_kind IN ('collection','legacy')
        ORDER BY l.link_id,r.started_at DESC,r.id DESC
        ON CONFLICT (link_id) DO UPDATE SET
            run_id=EXCLUDED.run_id,attempted_at=EXCLUDED.attempted_at,status=EXCLUDED.status,
            error_code=EXCLUDED.error_code,attempts=EXCLUDED.attempts,
            observation_updated=EXCLUDED.observation_updated
        WHERE road_traffic_attempt_latest.attempted_at < EXCLUDED.attempted_at
    """)
    op.execute("""
        ALTER TABLE population_latest ADD COLUMN received_at TIMESTAMP WITH TIME ZONE
    """)
    op.execute("""
        ALTER TABLE map_road_observations ADD COLUMN attempted_at TIMESTAMP WITH TIME ZONE
    """)
    op.execute("""
        ALTER TABLE map_road_observations ADD COLUMN attempt_status TEXT
    """)
    op.execute("""
        ALTER TABLE map_road_observations ADD COLUMN attempt_error_code TEXT
    """)
    op.execute("""
        ALTER TABLE map_population_observations ADD COLUMN received_at TIMESTAMP WITH TIME ZONE
    """)
    op.execute("""
        ALTER TABLE map_population_observations ADD COLUMN attempted_at TIMESTAMP WITH TIME ZONE
    """)
    op.execute("""
        ALTER TABLE map_population_observations ADD COLUMN attempt_status TEXT
    """)
    op.execute("""
        ALTER TABLE map_population_observations ADD COLUMN attempt_error_code TEXT
    """)
    op.execute("""
        CREATE TABLE collection_cursors (
            source TEXT NOT NULL,
            target_key TEXT NOT NULL,
            scan_id UUID NOT NULL,
            next_page INTEGER NOT NULL,
            page_size INTEGER NOT NULL,
            provider_total INTEGER,
            consistent_total BOOLEAN NOT NULL,
            pending_page INTEGER,
            pending_digest TEXT,
            pending_total INTEGER,
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
            CONSTRAINT pk_collection_cursors PRIMARY KEY (source, target_key),
            CONSTRAINT uq_collection_cursors_source UNIQUE (source, target_key, scan_id),
            CONSTRAINT ck_collection_cursors_target_key_nonblank CHECK (btrim(target_key) <>
        ''),
            CONSTRAINT ck_collection_cursors_next_page CHECK (next_page >= 1),
            CONSTRAINT ck_collection_cursors_page_size CHECK (page_size BETWEEN 1 AND 1000),
            CONSTRAINT ck_collection_cursors_totals CHECK (provider_total >= 0 AND pending_total
        >= 0),
            CONSTRAINT ck_collection_cursors_pending_page CHECK (pending_page >= 1),
            CONSTRAINT ck_collection_cursors_pending_digest CHECK (pending_digest ~
        '^[0-9a-f]{64}$'),
            CONSTRAINT ck_collection_cursors_pending_end CHECK ((pending_page IS NULL AND
        pending_digest IS NULL AND pending_total IS NULL) OR (pending_page IS NOT NULL AND
        pending_digest IS NOT NULL)),
            CONSTRAINT fk_collection_cursors_source_data_sources FOREIGN KEY(source) REFERENCES
        data_sources (source_code) ON DELETE RESTRICT
        )
    """)
    op.execute("""
        CREATE TABLE collection_cursor_pages (
            source TEXT NOT NULL,
            target_key TEXT NOT NULL,
            page INTEGER NOT NULL,
            scan_id UUID NOT NULL,
            digest TEXT NOT NULL,
            CONSTRAINT pk_collection_cursor_pages PRIMARY KEY (source, target_key, page),
            CONSTRAINT fk_collection_cursor_pages_source_collection_cursors FOREIGN KEY(source,
        target_key, scan_id) REFERENCES collection_cursors (source, target_key, scan_id) ON
        DELETE CASCADE,
            CONSTRAINT ck_collection_cursor_pages_page CHECK (page >= 1),
            CONSTRAINT ck_collection_cursor_pages_digest CHECK (digest ~ '^[0-9a-f]{64}$')
        )
    """)
    op.execute("""
        CREATE TABLE population_attempt_latest (
            area_code TEXT NOT NULL,
            attempted_at TIMESTAMP WITH TIME ZONE NOT NULL,
            status TEXT NOT NULL,
            error_code TEXT,
            observation_updated BOOLEAN NOT NULL,
            processor_version TEXT NOT NULL,
            CONSTRAINT pk_population_attempt_latest PRIMARY KEY (area_code),
            CONSTRAINT ck_population_attempt_latest_status CHECK (status IN
        ('success','partial','failed')),
            CONSTRAINT ck_population_attempt_latest_processor_version_nonblank CHECK
        (btrim(processor_version) <> ''),
            CONSTRAINT fk_population_attempt_latest_area_code_population_areas FOREIGN
        KEY(area_code) REFERENCES population_areas (area_code) ON DELETE RESTRICT
        )
    """)
    op.execute("""
        UPDATE population_latest t SET received_at=p.received_at FROM raw_payloads p WHERE
        t.payload_id=p.payload_id
    """)
    op.execute("""
        UPDATE map_population_observations t SET received_at=p.received_at FROM raw_payloads p
        WHERE t.payload_id=p.payload_id
    """)
    op.execute("""
        UPDATE map_road_observations t
        SET attempted_at=r.started_at, attempt_status=r.status, attempt_error_code=r.error_code
        FROM collection_runs r WHERE t.last_attempt_id=r.id
    """)
    op.execute("""
        UPDATE map_population_observations t
        SET attempted_at=r.started_at, attempt_status=r.status, attempt_error_code=r.error_code
        FROM collection_runs r WHERE t.last_attempt_id=r.id
    """)
    op.execute("""
        INSERT INTO population_attempt_latest
        (area_code,attempted_at,status,error_code,observation_updated,processor_version)
        SELECT DISTINCT ON (a.area_code)
            a.area_code,r.started_at,r.status,r.error_code,COALESCE(p.run_id=r.id,false),
            COALESCE(r.processor_version,'migration-0012')
        FROM population_areas a
        JOIN collection_runs r ON r.source='seoul_citydata'
            AND (r.target_key='area:' || a.area_code OR r.target_key='area_name:' || a.name)
        LEFT JOIN population_latest v ON v.area_code=a.area_code AND v.source='seoul_citydata'
        LEFT JOIN raw_payloads p ON p.payload_id=v.payload_id
        WHERE r.status IN ('success','partial','failed')
        ORDER BY a.area_code,r.started_at DESC,r.id DESC
    """)
    op.execute("""
        ALTER TABLE map_road_observations DROP CONSTRAINT
        ck_map_road_observations_source_payload_pair
    """)
    op.execute("""
        ALTER TABLE map_road_observations DROP CONSTRAINT
        ck_map_road_observations_observation_evidence
    """)
    op.execute("""
        ALTER TABLE map_population_observations DROP CONSTRAINT
        ck_map_population_observations_source_payload_pair
    """)
    op.execute("""
        ALTER TABLE map_population_observations DROP CONSTRAINT
        ck_map_population_observations_observation_evidence
    """)
    op.execute("""
        ALTER TABLE parking_availability_latest DROP CONSTRAINT
        ck_parking_availability_latest_observation
    """)
    op.execute("""
        ALTER TABLE parking_availability_latest DROP CONSTRAINT
        ck_parking_availability_latest_usable
    """)
    op.execute("""
        ALTER TABLE pedestrian_sensor_latest DROP CONSTRAINT
        ck_pedestrian_sensor_latest_observation_provenance
    """)
    op.execute("""
        ALTER TABLE population_areas DROP COLUMN last_payload_id
    """)
    op.execute("""
        ALTER TABLE population_latest DROP COLUMN payload_id
    """)
    op.execute("""
        ALTER TABLE administrative_areas DROP COLUMN last_payload_id
    """)
    op.execute("""
        ALTER TABLE region_boundaries DROP COLUMN last_payload_id
    """)
    op.execute("""
        ALTER TABLE map_road_observations DROP COLUMN payload_id
    """)
    op.execute("""
        ALTER TABLE map_road_observations ADD CONSTRAINT
        ck_map_road_observations_observation_evidence CHECK (NOT has_observation OR (source IS
        NOT NULL AND processor_version IS NOT NULL AND processed_at IS NOT NULL))
    """)
    op.execute("""
        ALTER TABLE map_road_observations ADD CONSTRAINT ck_map_road_observations_attempt_status
        CHECK (attempt_status IN ('success','partial','failed','running'))
    """)
    op.execute("""
        ALTER TABLE map_road_observations ADD CONSTRAINT ck_map_road_observations_attempt_pair
        CHECK ((attempted_at IS NULL) = (attempt_status IS NULL))
    """)
    op.execute("""
        ALTER TABLE map_road_observations ADD CONSTRAINT
        ck_map_road_observations_observation_provenance CHECK (num_nonnulls(source,
        processed_at, processor_version) IN (0,3))
    """)
    op.execute("""
        ALTER TABLE map_population_observations DROP COLUMN payload_id
    """)
    op.execute("""
        ALTER TABLE map_population_observations DROP CONSTRAINT
        fk_map_population_observations_last_attempt_id_collection_runs
    """)
    op.execute("""
        ALTER TABLE map_population_observations ADD CONSTRAINT
        fk_map_population_observations_last_attempt_id_collection_runs FOREIGN KEY
        (last_attempt_id) REFERENCES collection_runs (id) ON DELETE SET NULL
    """)
    op.execute("""
        ALTER TABLE map_population_observations ADD CONSTRAINT
        ck_map_population_observations_observation_evidence CHECK (NOT has_observation OR
        (source IS NOT NULL AND processor_version IS NOT NULL AND processed_at IS NOT NULL))
    """)
    op.execute("""
        ALTER TABLE map_population_observations ADD CONSTRAINT
        ck_map_population_observations_attempt_status CHECK (attempt_status IN
        ('success','partial','failed','running'))
    """)
    op.execute("""
        ALTER TABLE map_population_observations ADD CONSTRAINT
        ck_map_population_observations_attempt_pair CHECK ((attempted_at IS NULL) =
        (attempt_status IS NULL))
    """)
    op.execute("""
        ALTER TABLE map_population_observations ADD CONSTRAINT
        ck_map_population_observations_observation_provenance CHECK (num_nonnulls(source,
        processed_at, processor_version) IN (0,3))
    """)
    op.execute("""
        ALTER TABLE parking_source_records DROP COLUMN operation_payload_id
    """)
    op.execute("""
        ALTER TABLE parking_source_records DROP COLUMN last_payload_id
    """)
    op.execute("""
        ALTER TABLE parking_operating_hours DROP COLUMN payload_id
    """)
    op.execute("""
        ALTER TABLE parking_field_issues DROP COLUMN payload_id
    """)
    op.execute("""
        ALTER TABLE parking_availability_latest DROP COLUMN payload_id
    """)
    op.execute("""
        ALTER TABLE parking_availability_latest DROP COLUMN attempt_payload_id
    """)
    op.execute("""
        ALTER TABLE parking_availability_latest ADD CONSTRAINT
        ck_parking_availability_latest_usable CHECK (NOT usable OR observed_at IS NOT NULL)
    """)
    op.execute("""
        ALTER TABLE parking_availability_latest ADD CONSTRAINT
        ck_parking_availability_latest_absent_observation CHECK (observed_at IS NOT NULL OR
        num_nonnulls(total_spaces, occupied_spaces, reported_available_spaces,
        calculated_available_spaces, received_at, processed_at, processor_version) = 0)
    """)
    op.execute("""
        ALTER TABLE parking_availability_latest ADD CONSTRAINT
        ck_parking_availability_latest_observation CHECK (observed_at IS NULL OR (received_at IS
        NOT NULL AND total_spaces IS NOT NULL AND processed_at IS NOT NULL AND processor_version
        IS NOT NULL AND observed_at <= received_at AND ((method = 'reported' AND
        reported_available_spaces IS NOT NULL AND occupied_spaces IS NULL AND
        calculated_available_spaces IS NULL) OR (method = 'calculated' AND occupied_spaces IS
        NOT NULL AND calculated_available_spaces IS NOT NULL AND reported_available_spaces IS
        NULL AND calculated_available_spaces = total_spaces - occupied_spaces))))
    """)
    op.execute("""
        ALTER TABLE pedestrian_sensors DROP COLUMN reference_payload_id
    """)
    op.execute("""
        ALTER TABLE pedestrian_sensors DROP COLUMN boundary_payload_id
    """)
    op.execute("""
        ALTER TABLE pedestrian_sensors DROP COLUMN last_payload_id
    """)
    op.execute("""
        ALTER TABLE pedestrian_sensor_latest DROP COLUMN payload_id
    """)
    op.execute("""
        ALTER TABLE pedestrian_sensor_latest DROP COLUMN reference_payload_id
    """)
    op.execute("""
        ALTER TABLE pedestrian_sensor_latest DROP COLUMN attempt_payload_id
    """)
    op.execute("""
        ALTER TABLE pedestrian_sensor_latest ADD CONSTRAINT
        ck_pedestrian_sensor_latest_observation_provenance CHECK (num_nonnulls(count_10min,
        observed_at, data_no, received_at, provider_collected_at, reference_version,
        processed_at, processor_version, provider_sensor_id) IN (0,9))
    """)
    op.execute("""
        ALTER TABLE road_links DROP COLUMN last_payload_id
    """)
    op.execute("""
        ALTER TABLE road_link_mappings DROP COLUMN last_payload_id
    """)
    op.execute("""
        ALTER TABLE road_traffic_latest DROP COLUMN payload_id
    """)
    op.execute("""
        DROP TABLE payload_file_deletions
    """)
    op.execute("""
        DROP TABLE legacy_archives
    """)
    op.execute("""
        DROP TABLE raw_payloads
    """)


def downgrade():
    raise RuntimeError(
        "0012 removes response archives; discarded response contents cannot be reconstructed"
    )
