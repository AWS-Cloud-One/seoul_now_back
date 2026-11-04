"""Resolve both 0008 variants directly into the final relational service schema.

Revision ID: 0009
Revises: 0008

DDL and data conversion belong to this revision. Runtime model/converter changes
must not alter historical upgrades. Original rows are archived before removal;
the temporary legacy schema exists only inside the migration transaction.
"""

import hashlib
import json
import os
import re
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import UUID, uuid4
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateIndex, CreateTable

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

LEGACY_TABLES = (
    "collection_runs",
    "parking_records",
    "citydata_snapshots",
    "provider_snapshots",
    "population_areas",
    "floating_population_history",
    "external_api_responses",
    "external_api_observations",
    "map_layer_versions",
    "map_features",
    "map_observations",
    "parking_facilities",
    "parking_source_records",
)
TABLES = (
    "chat_sessions",
    "data_sources",
    "legacy_archives",
    "map_layer_versions",
    "parking_facilities",
    "place_categories",
    "chat_condition_versions",
    "collection_runs",
    "places",
    "chat_condition_age_groups",
    "chat_condition_purposes",
    "chat_excluded_places",
    "chat_messages",
    "collection_rejection_reasons",
    "raw_payloads",
    "administrative_areas",
    "parking_source_records",
    "population_areas",
    "region_boundaries",
    "road_links",
    "map_features",
    "population_latest",
    "road_area_mappings",
    "road_link_mappings",
    "road_traffic_latest",
    "map_population_observations",
    "map_road_observations",
)

# Fixed final DDL, not generated from application models at migration runtime.
SCHEMA_SQL = """
CREATE TABLE chat_sessions (
    trip_id UUID NOT NULL,
    condition_version INTEGER DEFAULT '0' NOT NULL,
    expires_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT pk_chat_sessions PRIMARY KEY (trip_id),
    CONSTRAINT ck_chat_sessions_condition_version CHECK (condition_version >= 0),
    CONSTRAINT ck_chat_sessions_update_order CHECK (updated_at >= created_at),
    CONSTRAINT ck_chat_sessions_expiry_order CHECK (expires_at > created_at)
);

CREATE TABLE data_sources (
    source_code TEXT NOT NULL,
    display_name TEXT NOT NULL,
    provider_name TEXT NOT NULL,
    reference_url TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT pk_data_sources PRIMARY KEY (source_code),
    CONSTRAINT ck_data_sources_source_code_nonblank CHECK (btrim(source_code) <> ''),
    CONSTRAINT ck_data_sources_display_name_nonblank CHECK (btrim(display_name) <> ''),
    CONSTRAINT ck_data_sources_provider_name_nonblank CHECK (btrim(provider_name) <> '')
);

CREATE TABLE legacy_archives (
    schema_name TEXT NOT NULL,
    table_name TEXT NOT NULL,
    source_revision TEXT NOT NULL,
    file_ref TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    byte_size BIGINT NOT NULL,
    row_count BIGINT NOT NULL,
    archived_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT pk_legacy_archives PRIMARY KEY (schema_name, table_name),
    CONSTRAINT ck_legacy_archives_schema_name_nonblank CHECK (btrim(schema_name) <> ''),
    CONSTRAINT ck_legacy_archives_table_name_nonblank CHECK (btrim(table_name) <> ''),
    CONSTRAINT ck_legacy_archives_file_ref_nonblank CHECK (btrim(file_ref) <> ''),
    CONSTRAINT ck_legacy_archives_source_revision_nonblank CHECK (btrim(source_revision) <> ''),
    CONSTRAINT ck_legacy_archives_sha256 CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_legacy_archives_counts CHECK (byte_size >= 0 AND row_count >= 0)
);

CREATE TABLE map_layer_versions (
    layer TEXT NOT NULL,
    geometry_version TEXT,
    is_ready BOOLEAN DEFAULT 'false' NOT NULL,
    coverage TEXT NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT pk_map_layer_versions PRIMARY KEY (layer),
    CONSTRAINT ck_map_layer_versions_geometry_version_nonblank CHECK (btrim(geometry_version) <>
    ''),
    CONSTRAINT ck_map_layer_versions_layer CHECK (layer IN ('roads','parking','population')),
    CONSTRAINT ck_map_layer_versions_ready_version CHECK (NOT is_ready OR (geometry_version IS NOT
    NULL AND btrim(geometry_version) <> ''))
);

CREATE TABLE parking_facilities (
    parking_id UUID NOT NULL,
    name TEXT NOT NULL,
    location geography(POINT,4326) NOT NULL,
    identity_status TEXT DEFAULT 'unverified' NOT NULL,
    is_active BOOLEAN DEFAULT 'true' NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT pk_parking_facilities PRIMARY KEY (parking_id),
    CONSTRAINT ck_parking_facilities_name_nonblank CHECK (btrim(name) <> ''),
    CONSTRAINT ck_parking_facilities_identity_status CHECK (identity_status IN ('verified',
    'unverified')),
    CONSTRAINT ck_parking_facilities_nonempty_location CHECK (NOT ST_IsEmpty(location::geometry))
);

CREATE TABLE place_categories (
    category_code TEXT NOT NULL,
    name TEXT NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT pk_place_categories PRIMARY KEY (category_code),
    CONSTRAINT ck_place_categories_category_code_nonblank CHECK (btrim(category_code) <> ''),
    CONSTRAINT ck_place_categories_name_nonblank CHECK (btrim(name) <> '')
);

CREATE TABLE chat_condition_versions (
    trip_id UUID NOT NULL,
    version INTEGER NOT NULL,
    origin_label TEXT,
    origin_location geography(POINT,4326),
    origin_address TEXT,
    origin_region TEXT,
    origin_supported BOOLEAN,
    group_type TEXT,
    headcount INTEGER,
    max_travel_seconds INTEGER,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT pk_chat_condition_versions PRIMARY KEY (trip_id, version),
    CONSTRAINT ck_chat_condition_versions_version CHECK (version >= 0),
    CONSTRAINT ck_chat_condition_versions_origin_label_nonblank CHECK (btrim(origin_label) <> ''),
    CONSTRAINT ck_chat_condition_versions_origin_fields CHECK ((num_nonnulls(origin_label,
    origin_location,origin_supported) = 0 AND origin_address IS NULL AND origin_region IS NULL) OR
    num_nonnulls(origin_label,origin_location,origin_supported) = 3),
    CONSTRAINT ck_chat_condition_versions_origin_region CHECK (origin_region IN ('seoul',
    'incheon','gyeonggi','other')),
    CONSTRAINT ck_chat_condition_versions_origin_nonempty CHECK (NOT
    ST_IsEmpty(origin_location::geometry)),
    CONSTRAINT ck_chat_condition_versions_group_type CHECK (group_type IN ('solo','date',
    'friends','family','other')),
    CONSTRAINT ck_chat_condition_versions_headcount CHECK (headcount > 0),
    CONSTRAINT ck_chat_condition_versions_max_travel_seconds CHECK (max_travel_seconds > 0),
    CONSTRAINT fk_chat_condition_versions_trip_id_chat_sessions FOREIGN KEY(trip_id) REFERENCES
    chat_sessions (trip_id) ON DELETE RESTRICT
);

CREATE TABLE collection_runs (
    id UUID NOT NULL,
    source TEXT NOT NULL,
    target_key TEXT NOT NULL,
    run_kind TEXT DEFAULT 'collection' NOT NULL,
    started_at TIMESTAMP WITH TIME ZONE NOT NULL,
    finished_at TIMESTAMP WITH TIME ZONE,
    status TEXT NOT NULL,
    completeness TEXT DEFAULT 'unknown' NOT NULL,
    received INTEGER DEFAULT '0' NOT NULL,
    accepted INTEGER DEFAULT '0' NOT NULL,
    rejected INTEGER DEFAULT '0' NOT NULL,
    duplicates INTEGER DEFAULT '0' NOT NULL,
    processor_version TEXT,
    error_code TEXT,
    CONSTRAINT pk_collection_runs PRIMARY KEY (id),
    CONSTRAINT ck_collection_runs_target_key_nonblank CHECK (btrim(target_key) <> ''),
    CONSTRAINT ck_collection_runs_processor_version_nonblank CHECK (btrim(processor_version) <>
    ''),
    CONSTRAINT ck_collection_runs_status CHECK (status IN ('running','success','partial',
    'failed')),
    CONSTRAINT ck_collection_runs_run_kind CHECK (run_kind IN ('collection','import','backfill',
    'reprocess','legacy')),
    CONSTRAINT ck_collection_runs_completeness CHECK (completeness IN ('complete','partial',
    'unknown')),
    CONSTRAINT ck_collection_runs_time_order CHECK (finished_at >= started_at),
    CONSTRAINT ck_collection_runs_counts_nonnegative CHECK (received >= 0 AND accepted >= 0 AND
    rejected >= 0 AND duplicates >= 0),
    CONSTRAINT fk_collection_runs_source_data_sources FOREIGN KEY(source) REFERENCES data_sources
    (source_code) ON DELETE RESTRICT
);

CREATE TABLE places (
    place_id TEXT NOT NULL,
    name TEXT NOT NULL,
    category_code TEXT,
    category_name TEXT,
    location geography(POINT,4326) NOT NULL,
    address TEXT,
    road_address TEXT,
    phone TEXT,
    provider_url TEXT,
    collected_at TIMESTAMP WITH TIME ZONE NOT NULL,
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    CONSTRAINT pk_places PRIMARY KEY (place_id),
    CONSTRAINT ck_places_name_nonblank CHECK (btrim(name) <> ''),
    CONSTRAINT ck_places_provider_identity CHECK (place_id ~ '^kakao:[^[:space:]]+$'),
    CONSTRAINT ck_places_expiry_order CHECK (expires_at > collected_at),
    CONSTRAINT ck_places_nonempty_location CHECK (NOT ST_IsEmpty(location::geometry)),
    CONSTRAINT fk_places_category_code_place_categories FOREIGN KEY(category_code) REFERENCES
    place_categories (category_code) ON DELETE RESTRICT
);

CREATE TABLE chat_condition_age_groups (
    trip_id UUID NOT NULL,
    condition_version INTEGER NOT NULL,
    age_group TEXT NOT NULL,
    position INTEGER NOT NULL,
    CONSTRAINT pk_chat_condition_age_groups PRIMARY KEY (trip_id, condition_version, age_group),
    CONSTRAINT fk_chat_condition_age_groups_trip_id_chat_condition_versions FOREIGN KEY(trip_id,
    condition_version) REFERENCES chat_condition_versions (trip_id, version) ON DELETE RESTRICT,
    CONSTRAINT uq_chat_condition_age_groups_trip_id UNIQUE (trip_id, condition_version, position),
    CONSTRAINT ck_chat_condition_age_groups_age_group CHECK (age_group IN ('under_10','teens',
    'twenties','thirties','forties','fifties','sixty_plus')),
    CONSTRAINT ck_chat_condition_age_groups_position CHECK (position BETWEEN 0 AND 6)
);

CREATE TABLE chat_condition_purposes (
    trip_id UUID NOT NULL,
    condition_version INTEGER NOT NULL,
    purpose TEXT NOT NULL,
    position INTEGER NOT NULL,
    keyword VARCHAR(100),
    CONSTRAINT pk_chat_condition_purposes PRIMARY KEY (trip_id, condition_version, purpose),
    CONSTRAINT fk_chat_condition_purposes_trip_id_chat_condition_versions FOREIGN KEY(trip_id,
    condition_version) REFERENCES chat_condition_versions (trip_id, version) ON DELETE RESTRICT,
    CONSTRAINT uq_chat_condition_purposes_trip_id UNIQUE (trip_id, condition_version, position),
    CONSTRAINT ck_chat_condition_purposes_purpose CHECK (purpose IN ('cafe','food','walk',
    'exhibition')),
    CONSTRAINT ck_chat_condition_purposes_position CHECK (position BETWEEN 0 AND 1)
);

CREATE TABLE chat_excluded_places (
    trip_id UUID NOT NULL,
    place_id TEXT NOT NULL,
    excluded_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT pk_chat_excluded_places PRIMARY KEY (trip_id, place_id),
    CONSTRAINT fk_chat_excluded_places_trip_id_chat_sessions FOREIGN KEY(trip_id) REFERENCES
    chat_sessions (trip_id) ON DELETE RESTRICT,
    CONSTRAINT fk_chat_excluded_places_place_id_places FOREIGN KEY(place_id) REFERENCES places
    (place_id) ON DELETE RESTRICT
);

CREATE TABLE chat_messages (
    message_id UUID NOT NULL,
    trip_id UUID NOT NULL,
    sequence INTEGER NOT NULL,
    request_id UUID,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    kind TEXT,
    base_condition_version INTEGER,
    condition_version INTEGER,
    condition_applied BOOLEAN,
    interpretation_status TEXT,
    routes_status TEXT,
    explanation_status TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT pk_chat_messages PRIMARY KEY (message_id),
    CONSTRAINT uq_chat_messages_trip_id UNIQUE (trip_id, sequence),
    CONSTRAINT fk_chat_messages_trip_id_chat_condition_versions FOREIGN KEY(trip_id,
    condition_version) REFERENCES chat_condition_versions (trip_id, version) ON DELETE RESTRICT,
    CONSTRAINT fk_chat_messages_base_condition FOREIGN KEY(trip_id, base_condition_version)
    REFERENCES chat_condition_versions (trip_id, version) ON DELETE RESTRICT,
    CONSTRAINT ck_chat_messages_sequence CHECK (sequence >= 1),
    CONSTRAINT ck_chat_messages_role CHECK (role IN ('user','assistant')),
    CONSTRAINT ck_chat_messages_kind CHECK (kind IN ('clarification','recommendation',
    'general_candidates','no_candidates','failure','answer')),
    CONSTRAINT ck_chat_messages_assistant_kind CHECK (kind IS NULL OR role = 'assistant'),
    CONSTRAINT ck_chat_messages_applied_versions CHECK (condition_applied IS NULL OR
    (base_condition_version IS NOT NULL AND condition_version IS NOT NULL)),
    CONSTRAINT ck_chat_messages_version_order CHECK (condition_version >= base_condition_version),
    CONSTRAINT ck_chat_messages_unapplied_version CHECK (condition_applied IS DISTINCT FROM FALSE
    OR condition_version = base_condition_version),
    CONSTRAINT ck_chat_messages_interpretation_status CHECK (interpretation_status IN ('skipped',
    'ok','failed')),
    CONSTRAINT ck_chat_messages_routes_status CHECK (routes_status IN ('skipped','ok','partial',
    'failed')),
    CONSTRAINT ck_chat_messages_explanation_status CHECK (explanation_status IN ('skipped','ok',
    'fallback')),
    CONSTRAINT fk_chat_messages_trip_id_chat_sessions FOREIGN KEY(trip_id) REFERENCES
    chat_sessions (trip_id) ON DELETE RESTRICT
);

CREATE TABLE collection_rejection_reasons (
    run_id UUID NOT NULL,
    reason_code TEXT NOT NULL,
    occurrence_count INTEGER NOT NULL,
    CONSTRAINT pk_collection_rejection_reasons PRIMARY KEY (run_id, reason_code),
    CONSTRAINT ck_collection_rejection_reasons_reason_code_nonblank CHECK (btrim(reason_code) <>
    ''),
    CONSTRAINT ck_collection_rejection_reasons_positive_count CHECK (occurrence_count > 0),
    CONSTRAINT fk_collection_rejection_reasons_run_id_collection_runs FOREIGN KEY(run_id)
    REFERENCES collection_runs (id) ON DELETE RESTRICT
);

CREATE TABLE raw_payloads (
    payload_id UUID NOT NULL,
    run_id UUID NOT NULL,
    part_key TEXT NOT NULL,
    payload_kind TEXT NOT NULL,
    format TEXT NOT NULL,
    file_ref TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    byte_size BIGINT NOT NULL,
    document_type TEXT NOT NULL,
    parent_payload_id UUID,
    received_at TIMESTAMP WITH TIME ZONE,
    stored_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    source_version TEXT,
    reference_date DATE,
    legacy_table TEXT,
    legacy_key TEXT,
    legacy_collected_at TIMESTAMP WITH TIME ZONE,
    legacy_time_basis TEXT,
    CONSTRAINT pk_raw_payloads PRIMARY KEY (payload_id),
    CONSTRAINT uq_raw_payloads_run_id UNIQUE (run_id, part_key),
    CONSTRAINT uq_raw_payloads_legacy_table UNIQUE (legacy_table, legacy_key),
    CONSTRAINT ck_raw_payloads_part_key_nonblank CHECK (btrim(part_key) <> ''),
    CONSTRAINT ck_raw_payloads_format_nonblank CHECK (btrim(format) <> ''),
    CONSTRAINT ck_raw_payloads_file_ref_nonblank CHECK (btrim(file_ref) <> ''),
    CONSTRAINT ck_raw_payloads_legacy_table_nonblank CHECK (btrim(legacy_table) <> ''),
    CONSTRAINT ck_raw_payloads_legacy_key_nonblank CHECK (btrim(legacy_key) <> ''),
    CONSTRAINT ck_raw_payloads_kind CHECK (payload_kind IN ('provider_response','source_file',
    'derived_snapshot','processing_report','legacy_snapshot','legacy_observation')),
    CONSTRAINT ck_raw_payloads_sha256 CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_raw_payloads_byte_size CHECK (byte_size >= 0),
    CONSTRAINT ck_raw_payloads_document_type CHECK (document_type IN ('object','array','scalar',
    'text','binary')),
    CONSTRAINT ck_raw_payloads_legacy_identity_pair CHECK ((legacy_table IS NULL) = (legacy_key IS
    NULL)),
    CONSTRAINT ck_raw_payloads_legacy_time_pair CHECK ((legacy_collected_at IS NULL) =
    (legacy_time_basis IS NULL)),
    CONSTRAINT ck_raw_payloads_legacy_time_basis CHECK (legacy_time_basis IN ('run_started',
    'snapshot_saved','unknown')),
    CONSTRAINT ck_raw_payloads_not_own_parent CHECK (parent_payload_id <> payload_id),
    CONSTRAINT fk_raw_payloads_run_id_collection_runs FOREIGN KEY(run_id) REFERENCES
    collection_runs (id) ON DELETE RESTRICT,
    CONSTRAINT fk_raw_payloads_parent_payload_id_raw_payloads FOREIGN KEY(parent_payload_id)
    REFERENCES raw_payloads (payload_id) ON DELETE RESTRICT
);

CREATE TABLE administrative_areas (
    dong_code TEXT NOT NULL,
    name TEXT NOT NULL,
    sigungu_code TEXT NOT NULL,
    code_system TEXT NOT NULL,
    geometry geometry(MULTIPOLYGON,4326) NOT NULL,
    boundary_version TEXT NOT NULL,
    road_mapping_version TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    source TEXT NOT NULL,
    source_version TEXT,
    reference_date DATE,
    last_payload_id UUID,
    processor_version TEXT,
    processed_at TIMESTAMP WITH TIME ZONE,
    is_active BOOLEAN DEFAULT 'true' NOT NULL,
    CONSTRAINT pk_administrative_areas PRIMARY KEY (dong_code),
    CONSTRAINT ck_administrative_areas_processor_version_nonblank CHECK (btrim(processor_version)
    <> ''),
    CONSTRAINT ck_administrative_areas_processing_pair CHECK ((processor_version IS NULL) =
    (processed_at IS NULL)),
    CONSTRAINT ck_administrative_areas_dong_code_nonblank CHECK (btrim(dong_code) <> ''),
    CONSTRAINT ck_administrative_areas_name_nonblank CHECK (btrim(name) <> ''),
    CONSTRAINT ck_administrative_areas_sigungu_code_nonblank CHECK (btrim(sigungu_code) <> ''),
    CONSTRAINT ck_administrative_areas_code_system_nonblank CHECK (btrim(code_system) <> ''),
    CONSTRAINT ck_administrative_areas_boundary_version_nonblank CHECK (btrim(boundary_version) <>
    ''),
    CONSTRAINT ck_administrative_areas_road_mapping_version_nonblank CHECK
    (btrim(road_mapping_version) <> ''),
    CONSTRAINT ck_administrative_areas_valid_polygon CHECK (NOT ST_IsEmpty(geometry) AND
    ST_IsValid(geometry)),
    CONSTRAINT fk_administrative_areas_source_data_sources FOREIGN KEY(source) REFERENCES
    data_sources (source_code) ON DELETE RESTRICT,
    CONSTRAINT fk_administrative_areas_last_payload_id_raw_payloads FOREIGN KEY(last_payload_id)
    REFERENCES raw_payloads (payload_id) ON DELETE RESTRICT
);

CREATE TABLE parking_source_records (
    source_record_id UUID NOT NULL,
    parking_id UUID NOT NULL,
    legacy_record_id TEXT,
    provider_record_id TEXT,
    name TEXT,
    location geography(POINT,4326),
    address TEXT,
    registered_capacity INTEGER,
    base_minutes NUMERIC,
    base_fee_krw NUMERIC,
    additional_minutes NUMERIC,
    additional_fee_krw NUMERIC,
    daily_max_krw NUMERIC,
    fee_notes TEXT,
    weekday_hours TEXT,
    saturday_hours TEXT,
    sunday_holiday_hours TEXT,
    hours_notes TEXT,
    source_updated_at TIMESTAMP WITH TIME ZONE,
    collected_at TIMESTAMP WITH TIME ZONE,
    reason_code TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    source TEXT NOT NULL,
    source_version TEXT,
    reference_date DATE,
    last_payload_id UUID,
    processor_version TEXT,
    processed_at TIMESTAMP WITH TIME ZONE,
    is_active BOOLEAN DEFAULT 'true' NOT NULL,
    CONSTRAINT pk_parking_source_records PRIMARY KEY (source_record_id),
    CONSTRAINT ck_parking_source_records_processor_version_nonblank CHECK
    (btrim(processor_version) <> ''),
    CONSTRAINT ck_parking_source_records_processing_pair CHECK ((processor_version IS NULL) =
    (processed_at IS NULL)),
    CONSTRAINT ck_parking_source_records_legacy_record_id_nonblank CHECK (btrim(legacy_record_id)
    <> ''),
    CONSTRAINT ck_parking_source_records_provider_record_id_nonblank CHECK
    (btrim(provider_record_id) <> ''),
    CONSTRAINT ck_parking_source_records_name_nonblank CHECK (btrim(name) <> ''),
    CONSTRAINT ck_parking_source_records_capacity_nonnegative CHECK (registered_capacity >= 0),
    CONSTRAINT ck_parking_source_records_nonempty_location CHECK (NOT
    ST_IsEmpty(location::geometry)),
    CONSTRAINT ck_parking_source_records_base_minutes_finite CHECK (base_minutes >= 0 AND
    base_minutes < 'Infinity'::numeric),
    CONSTRAINT ck_parking_source_records_base_fee_krw_finite CHECK (base_fee_krw >= 0 AND
    base_fee_krw < 'Infinity'::numeric),
    CONSTRAINT ck_parking_source_records_additional_minutes_finite CHECK (additional_minutes >= 0
    AND additional_minutes < 'Infinity'::numeric),
    CONSTRAINT ck_parking_source_records_additional_fee_krw_finite CHECK (additional_fee_krw >= 0
    AND additional_fee_krw < 'Infinity'::numeric),
    CONSTRAINT ck_parking_source_records_daily_max_krw_finite CHECK (daily_max_krw >= 0 AND
    daily_max_krw < 'Infinity'::numeric),
    CONSTRAINT fk_parking_source_records_parking_id_parking_facilities FOREIGN KEY(parking_id)
    REFERENCES parking_facilities (parking_id) ON DELETE RESTRICT,
    CONSTRAINT uq_parking_source_records_legacy_record_id UNIQUE (legacy_record_id),
    CONSTRAINT fk_parking_source_records_source_data_sources FOREIGN KEY(source) REFERENCES
    data_sources (source_code) ON DELETE RESTRICT,
    CONSTRAINT fk_parking_source_records_last_payload_id_raw_payloads FOREIGN KEY(last_payload_id)
    REFERENCES raw_payloads (payload_id) ON DELETE RESTRICT
);

CREATE TABLE population_areas (
    area_code TEXT NOT NULL,
    name TEXT NOT NULL,
    latitude DOUBLE PRECISION,
    longitude DOUBLE PRECISION,
    district TEXT,
    geometry geometry(MULTIPOLYGON,4326) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    source TEXT NOT NULL,
    source_version TEXT,
    reference_date DATE,
    last_payload_id UUID,
    processor_version TEXT,
    processed_at TIMESTAMP WITH TIME ZONE,
    is_active BOOLEAN DEFAULT 'true' NOT NULL,
    CONSTRAINT pk_population_areas PRIMARY KEY (area_code),
    CONSTRAINT ck_population_areas_area_code_nonblank CHECK (btrim(area_code) <> ''),
    CONSTRAINT ck_population_areas_name_nonblank CHECK (btrim(name) <> ''),
    CONSTRAINT ck_population_areas_processor_version_nonblank CHECK (btrim(processor_version) <>
    ''),
    CONSTRAINT ck_population_areas_processing_pair CHECK ((processor_version IS NULL) =
    (processed_at IS NULL)),
    CONSTRAINT ck_population_areas_valid_polygon CHECK (NOT ST_IsEmpty(geometry) AND
    ST_IsValid(geometry)),
    CONSTRAINT ck_population_areas_latitude CHECK (latitude BETWEEN -90 AND 90),
    CONSTRAINT ck_population_areas_longitude CHECK (longitude BETWEEN -180 AND 180),
    CONSTRAINT ck_population_areas_coordinate_pair CHECK ((latitude IS NULL) = (longitude IS
    NULL)),
    CONSTRAINT fk_population_areas_source_data_sources FOREIGN KEY(source) REFERENCES data_sources
    (source_code) ON DELETE RESTRICT,
    CONSTRAINT fk_population_areas_last_payload_id_raw_payloads FOREIGN KEY(last_payload_id)
    REFERENCES raw_payloads (payload_id) ON DELETE RESTRICT
);

CREATE TABLE region_boundaries (
    code_system TEXT NOT NULL,
    region_code TEXT NOT NULL,
    name TEXT NOT NULL,
    geometry geometry(MULTIPOLYGON,4326) NOT NULL,
    boundary_version TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    source TEXT NOT NULL,
    source_version TEXT,
    reference_date DATE,
    last_payload_id UUID,
    processor_version TEXT,
    processed_at TIMESTAMP WITH TIME ZONE,
    is_active BOOLEAN DEFAULT 'true' NOT NULL,
    CONSTRAINT pk_region_boundaries PRIMARY KEY (code_system, region_code),
    CONSTRAINT ck_region_boundaries_processor_version_nonblank CHECK (btrim(processor_version) <>
    ''),
    CONSTRAINT ck_region_boundaries_processing_pair CHECK ((processor_version IS NULL) =
    (processed_at IS NULL)),
    CONSTRAINT ck_region_boundaries_code_system_nonblank CHECK (btrim(code_system) <> ''),
    CONSTRAINT ck_region_boundaries_region_code_nonblank CHECK (btrim(region_code) <> ''),
    CONSTRAINT ck_region_boundaries_name_nonblank CHECK (btrim(name) <> ''),
    CONSTRAINT ck_region_boundaries_boundary_version_nonblank CHECK (btrim(boundary_version) <>
    ''),
    CONSTRAINT ck_region_boundaries_valid_polygon CHECK (NOT ST_IsEmpty(geometry) AND
    ST_IsValid(geometry)),
    CONSTRAINT fk_region_boundaries_source_data_sources FOREIGN KEY(source) REFERENCES
    data_sources (source_code) ON DELETE RESTRICT,
    CONSTRAINT fk_region_boundaries_last_payload_id_raw_payloads FOREIGN KEY(last_payload_id)
    REFERENCES raw_payloads (payload_id) ON DELETE RESTRICT
);

CREATE TABLE road_links (
    link_id TEXT NOT NULL,
    name TEXT,
    direction TEXT,
    geometry geometry(MULTILINESTRING,4326) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    source TEXT NOT NULL,
    source_version TEXT,
    reference_date DATE,
    last_payload_id UUID,
    processor_version TEXT,
    processed_at TIMESTAMP WITH TIME ZONE,
    is_active BOOLEAN DEFAULT 'true' NOT NULL,
    CONSTRAINT pk_road_links PRIMARY KEY (link_id),
    CONSTRAINT ck_road_links_processor_version_nonblank CHECK (btrim(processor_version) <> ''),
    CONSTRAINT ck_road_links_processing_pair CHECK ((processor_version IS NULL) = (processed_at IS
    NULL)),
    CONSTRAINT ck_road_links_link_id_nonblank CHECK (btrim(link_id) <> ''),
    CONSTRAINT ck_road_links_valid_line CHECK (NOT ST_IsEmpty(geometry) AND ST_IsValid(geometry)
    AND ST_Length(geometry) > 0),
    CONSTRAINT fk_road_links_source_data_sources FOREIGN KEY(source) REFERENCES data_sources
    (source_code) ON DELETE RESTRICT,
    CONSTRAINT fk_road_links_last_payload_id_raw_payloads FOREIGN KEY(last_payload_id) REFERENCES
    raw_payloads (payload_id) ON DELETE RESTRICT
);

CREATE TABLE map_features (
    layer TEXT NOT NULL,
    feature_id TEXT NOT NULL,
    road_link_id TEXT,
    population_area_code TEXT,
    parking_id UUID,
    geometry geometry(GEOMETRY,4326) NOT NULL,
    name TEXT,
    direction TEXT,
    identity_status TEXT,
    is_active BOOLEAN DEFAULT 'true' NOT NULL,
    published_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT pk_map_features PRIMARY KEY (layer, feature_id),
    CONSTRAINT ck_map_features_feature_id_nonblank CHECK (btrim(feature_id) <> ''),
    CONSTRAINT ck_map_features_name_nonblank CHECK (btrim(name) <> ''),
    CONSTRAINT ck_map_features_valid_geometry CHECK (NOT ST_IsEmpty(geometry) AND
    ST_IsValid(geometry)),
    CONSTRAINT ck_map_features_layer_identity_geometry CHECK ((layer = 'roads' AND road_link_id IS
    NOT NULL AND road_link_id = feature_id AND population_area_code IS NULL AND parking_id IS NULL
    AND identity_status IS NULL AND ST_GeometryType(geometry) = 'ST_MultiLineString') OR (layer =
    'population' AND population_area_code IS NOT NULL AND population_area_code = feature_id AND
    road_link_id IS NULL AND parking_id IS NULL AND name IS NOT NULL AND identity_status IS NULL
    AND direction IS NULL AND ST_GeometryType(geometry) = 'ST_MultiPolygon') OR (layer = 'parking'
    AND parking_id IS NOT NULL AND parking_id::text = feature_id AND road_link_id IS NULL AND
    population_area_code IS NULL AND name IS NOT NULL AND direction IS NULL AND identity_status IS
    NOT NULL AND identity_status IN ('verified','unverified') AND ST_GeometryType(geometry) =
    'ST_Point')),
    CONSTRAINT fk_map_features_layer_map_layer_versions FOREIGN KEY(layer) REFERENCES
    map_layer_versions (layer) ON DELETE RESTRICT,
    CONSTRAINT fk_map_features_road_link_id_road_links FOREIGN KEY(road_link_id) REFERENCES
    road_links (link_id) ON DELETE RESTRICT,
    CONSTRAINT fk_map_features_population_area_code_population_areas FOREIGN
    KEY(population_area_code) REFERENCES population_areas (area_code) ON DELETE RESTRICT,
    CONSTRAINT fk_map_features_parking_id_parking_facilities FOREIGN KEY(parking_id) REFERENCES
    parking_facilities (parking_id) ON DELETE RESTRICT
);

CREATE TABLE population_latest (
    source TEXT NOT NULL,
    area_code TEXT NOT NULL,
    min_people INTEGER,
    max_people INTEGER,
    congestion TEXT,
    is_replacement BOOLEAN,
    payload_id UUID NOT NULL,
    observed_at TIMESTAMP WITH TIME ZONE,
    processed_at TIMESTAMP WITH TIME ZONE NOT NULL,
    processor_version TEXT NOT NULL,
    reason_code TEXT,
    CONSTRAINT pk_population_latest PRIMARY KEY (source, area_code),
    CONSTRAINT ck_population_latest_processor_version_nonblank CHECK (btrim(processor_version) <>
    ''),
    CONSTRAINT ck_population_latest_people_nonnegative CHECK (min_people >= 0 AND max_people >=
    0),
    CONSTRAINT ck_population_latest_people_order CHECK (min_people <= max_people),
    CONSTRAINT ck_population_latest_congestion CHECK (congestion IN ('relaxed','normal',
    'slightly_crowded','crowded')),
    CONSTRAINT fk_population_latest_source_data_sources FOREIGN KEY(source) REFERENCES
    data_sources (source_code) ON DELETE RESTRICT,
    CONSTRAINT fk_population_latest_area_code_population_areas FOREIGN KEY(area_code) REFERENCES
    population_areas (area_code) ON DELETE RESTRICT,
    CONSTRAINT fk_population_latest_payload_id_raw_payloads FOREIGN KEY(payload_id) REFERENCES
    raw_payloads (payload_id) ON DELETE RESTRICT
);

CREATE TABLE road_area_mappings (
    link_id TEXT NOT NULL,
    dong_code TEXT NOT NULL,
    boundary_version TEXT NOT NULL,
    road_geometry_version TEXT NOT NULL,
    processor_version TEXT NOT NULL,
    mapped_at TIMESTAMP WITH TIME ZONE NOT NULL,
    CONSTRAINT pk_road_area_mappings PRIMARY KEY (link_id, dong_code),
    CONSTRAINT ck_road_area_mappings_boundary_version_nonblank CHECK (btrim(boundary_version) <>
    ''),
    CONSTRAINT ck_road_area_mappings_road_geometry_version_nonblank CHECK
    (btrim(road_geometry_version) <> ''),
    CONSTRAINT ck_road_area_mappings_processor_version_nonblank CHECK (btrim(processor_version) <>
    ''),
    CONSTRAINT fk_road_area_mappings_link_id_road_links FOREIGN KEY(link_id) REFERENCES road_links
    (link_id) ON DELETE RESTRICT,
    CONSTRAINT fk_road_area_mappings_dong_code_administrative_areas FOREIGN KEY(dong_code)
    REFERENCES administrative_areas (dong_code) ON DELETE RESTRICT
);

CREATE TABLE road_link_mappings (
    service_link_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    standard_link_id TEXT NOT NULL,
    source TEXT NOT NULL,
    source_version TEXT NOT NULL,
    reference_date DATE,
    last_payload_id UUID,
    processor_version TEXT,
    processed_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT pk_road_link_mappings PRIMARY KEY (service_link_id, sequence, standard_link_id),
    CONSTRAINT ck_road_link_mappings_processor_version_nonblank CHECK (btrim(processor_version) <>
    ''),
    CONSTRAINT ck_road_link_mappings_processing_pair CHECK ((processor_version IS NULL) =
    (processed_at IS NULL)),
    CONSTRAINT ck_road_link_mappings_standard_link_id_nonblank CHECK (btrim(standard_link_id) <>
    ''),
    CONSTRAINT ck_road_link_mappings_source_version_nonblank CHECK (btrim(source_version) <> ''),
    CONSTRAINT ck_road_link_mappings_sequence_nonnegative CHECK (sequence >= 0),
    CONSTRAINT fk_road_link_mappings_service_link_id_road_links FOREIGN KEY(service_link_id)
    REFERENCES road_links (link_id) ON DELETE RESTRICT,
    CONSTRAINT fk_road_link_mappings_source_data_sources FOREIGN KEY(source) REFERENCES
    data_sources (source_code) ON DELETE RESTRICT,
    CONSTRAINT fk_road_link_mappings_last_payload_id_raw_payloads FOREIGN KEY(last_payload_id)
    REFERENCES raw_payloads (payload_id) ON DELETE RESTRICT
);

CREATE TABLE road_traffic_latest (
    source TEXT NOT NULL,
    link_id TEXT NOT NULL,
    speed_kmh NUMERIC,
    travel_seconds NUMERIC,
    payload_id UUID NOT NULL,
    observed_at TIMESTAMP WITH TIME ZONE,
    processed_at TIMESTAMP WITH TIME ZONE NOT NULL,
    processor_version TEXT NOT NULL,
    reason_code TEXT,
    CONSTRAINT pk_road_traffic_latest PRIMARY KEY (source, link_id),
    CONSTRAINT ck_road_traffic_latest_processor_version_nonblank CHECK (btrim(processor_version)
    <> ''),
    CONSTRAINT ck_road_traffic_latest_speed_kmh_finite CHECK (speed_kmh >= 0 AND speed_kmh <
    'Infinity'::numeric),
    CONSTRAINT ck_road_traffic_latest_travel_seconds_finite CHECK (travel_seconds >= 0 AND
    travel_seconds < 'Infinity'::numeric),
    CONSTRAINT fk_road_traffic_latest_source_data_sources FOREIGN KEY(source) REFERENCES
    data_sources (source_code) ON DELETE RESTRICT,
    CONSTRAINT fk_road_traffic_latest_link_id_road_links FOREIGN KEY(link_id) REFERENCES
    road_links (link_id) ON DELETE RESTRICT,
    CONSTRAINT fk_road_traffic_latest_payload_id_raw_payloads FOREIGN KEY(payload_id) REFERENCES
    raw_payloads (payload_id) ON DELETE RESTRICT
);

CREATE TABLE map_population_observations (
    area_code TEXT NOT NULL,
    layer TEXT DEFAULT 'population' NOT NULL,
    min_people INTEGER,
    max_people INTEGER,
    congestion TEXT,
    is_replacement BOOLEAN,
    source TEXT,
    payload_id UUID,
    has_observation BOOLEAN DEFAULT 'false' NOT NULL,
    observed_at TIMESTAMP WITH TIME ZONE,
    processed_at TIMESTAMP WITH TIME ZONE,
    processor_version TEXT,
    reason_code TEXT,
    last_attempt_id UUID,
    collection_failed BOOLEAN DEFAULT 'false' NOT NULL,
    published_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT pk_map_population_observations PRIMARY KEY (area_code),
    CONSTRAINT ck_map_population_observations_layer CHECK (layer = 'population'),
    CONSTRAINT fk_map_population_observations_layer_map_features FOREIGN KEY(layer, area_code)
    REFERENCES map_features (layer, feature_id) ON DELETE RESTRICT,
    CONSTRAINT ck_map_population_observations_processor_version_nonblank CHECK
    (btrim(processor_version) <> ''),
    CONSTRAINT ck_map_population_observations_observation_evidence CHECK (NOT has_observation OR
    (payload_id IS NOT NULL AND source IS NOT NULL AND processor_version IS NOT NULL AND
    processed_at IS NOT NULL)),
    CONSTRAINT ck_map_population_observations_absent_values CHECK (has_observation OR (min_people
    IS NULL AND max_people IS NULL AND congestion IS NULL AND is_replacement IS NULL)),
    CONSTRAINT ck_map_population_observations_source_payload_pair CHECK ((payload_id IS NULL) =
    (source IS NULL)),
    CONSTRAINT ck_map_population_observations_people_nonnegative CHECK (min_people >= 0 AND
    max_people >= 0),
    CONSTRAINT ck_map_population_observations_people_order CHECK (min_people <= max_people),
    CONSTRAINT ck_map_population_observations_congestion CHECK (congestion IN ('relaxed','normal',
    'slightly_crowded','crowded')),
    CONSTRAINT fk_map_population_observations_source_data_sources FOREIGN KEY(source) REFERENCES
    data_sources (source_code) ON DELETE RESTRICT,
    CONSTRAINT fk_map_population_observations_payload_id_raw_payloads FOREIGN KEY(payload_id)
    REFERENCES raw_payloads (payload_id) ON DELETE RESTRICT,
    CONSTRAINT fk_map_population_observations_last_attempt_id_collection_runs FOREIGN
    KEY(last_attempt_id) REFERENCES collection_runs (id) ON DELETE RESTRICT
);

CREATE TABLE map_road_observations (
    link_id TEXT NOT NULL,
    layer TEXT DEFAULT 'roads' NOT NULL,
    speed_kmh NUMERIC,
    travel_seconds NUMERIC,
    source TEXT,
    payload_id UUID,
    has_observation BOOLEAN DEFAULT 'false' NOT NULL,
    observed_at TIMESTAMP WITH TIME ZONE,
    processed_at TIMESTAMP WITH TIME ZONE,
    processor_version TEXT,
    reason_code TEXT,
    last_attempt_id UUID,
    collection_failed BOOLEAN DEFAULT 'false' NOT NULL,
    published_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT pk_map_road_observations PRIMARY KEY (link_id),
    CONSTRAINT ck_map_road_observations_layer CHECK (layer = 'roads'),
    CONSTRAINT fk_map_road_observations_layer_map_features FOREIGN KEY(layer, link_id) REFERENCES
    map_features (layer, feature_id) ON DELETE RESTRICT,
    CONSTRAINT ck_map_road_observations_processor_version_nonblank CHECK (btrim(processor_version)
    <> ''),
    CONSTRAINT ck_map_road_observations_observation_evidence CHECK (NOT has_observation OR
    (payload_id IS NOT NULL AND source IS NOT NULL AND processor_version IS NOT NULL AND
    processed_at IS NOT NULL)),
    CONSTRAINT ck_map_road_observations_absent_values CHECK (has_observation OR (speed_kmh IS NULL
    AND travel_seconds IS NULL)),
    CONSTRAINT ck_map_road_observations_source_payload_pair CHECK ((payload_id IS NULL) = (source
    IS NULL)),
    CONSTRAINT ck_map_road_observations_speed_kmh_finite CHECK (speed_kmh >= 0 AND speed_kmh <
    'Infinity'::numeric),
    CONSTRAINT ck_map_road_observations_travel_seconds_finite CHECK (travel_seconds >= 0 AND
    travel_seconds < 'Infinity'::numeric),
    CONSTRAINT fk_map_road_observations_source_data_sources FOREIGN KEY(source) REFERENCES
    data_sources (source_code) ON DELETE RESTRICT,
    CONSTRAINT fk_map_road_observations_payload_id_raw_payloads FOREIGN KEY(payload_id) REFERENCES
    raw_payloads (payload_id) ON DELETE RESTRICT,
    CONSTRAINT fk_map_road_observations_last_attempt_id_collection_runs FOREIGN
    KEY(last_attempt_id) REFERENCES collection_runs (id) ON DELETE RESTRICT
);

ALTER TABLE chat_sessions ADD CONSTRAINT fk_chat_sessions_current_condition FOREIGN KEY(trip_id,
    condition_version) REFERENCES chat_condition_versions (trip_id, version) DEFERRABLE INITIALLY
    DEFERRED;

CREATE INDEX idx_parking_facilities_location ON parking_facilities USING gist (location);

CREATE INDEX idx_chat_condition_versions_origin_location ON chat_condition_versions USING gist
    (origin_location);

CREATE INDEX ix_collection_runs_source_target_started ON collection_runs (source, target_key,
    started_at DESC);

CREATE INDEX idx_places_location ON places USING gist (location);

CREATE INDEX ix_places_category_code ON places (category_code);

CREATE INDEX ix_chat_excluded_places_place_id ON chat_excluded_places (place_id);

CREATE INDEX ix_chat_messages_request_id ON chat_messages (request_id);

CREATE INDEX ix_raw_payloads_parent_payload_id ON raw_payloads (parent_payload_id);

CREATE INDEX ix_raw_payloads_run_id ON raw_payloads (run_id);

CREATE INDEX idx_administrative_areas_geometry ON administrative_areas USING gist (geometry);

CREATE INDEX ix_administrative_areas_last_payload_id ON administrative_areas (last_payload_id);

CREATE INDEX ix_administrative_areas_sigungu_code ON administrative_areas (sigungu_code);

CREATE INDEX idx_parking_source_records_location ON parking_source_records USING gist (location);

CREATE INDEX ix_parking_source_records_last_payload_id ON parking_source_records
    (last_payload_id);

CREATE INDEX ix_parking_source_records_parking_id ON parking_source_records (parking_id);

CREATE INDEX ix_parking_source_records_provider_record_id ON parking_source_records
    (provider_record_id);

CREATE INDEX idx_population_areas_geometry ON population_areas USING gist (geometry);

CREATE INDEX ix_population_areas_district ON population_areas (district);

CREATE INDEX ix_population_areas_last_payload_id ON population_areas (last_payload_id);

CREATE INDEX idx_region_boundaries_geometry ON region_boundaries USING gist (geometry);

CREATE INDEX ix_region_boundaries_last_payload_id ON region_boundaries (last_payload_id);

CREATE INDEX idx_road_links_geometry ON road_links USING gist (geometry);

CREATE INDEX ix_road_links_last_payload_id ON road_links (last_payload_id);

CREATE INDEX idx_map_features_geometry ON map_features USING gist (geometry);

CREATE INDEX ix_population_latest_payload_id ON population_latest (payload_id);

CREATE INDEX ix_road_area_mappings_dong_code ON road_area_mappings (dong_code);

CREATE INDEX ix_road_link_mappings_last_payload_id ON road_link_mappings (last_payload_id);

CREATE INDEX ix_road_link_mappings_standard_link_id ON road_link_mappings (standard_link_id);

CREATE INDEX ix_road_traffic_latest_payload_id ON road_traffic_latest (payload_id);

CREATE INDEX ix_map_population_observations_last_attempt_id ON map_population_observations
    (last_attempt_id);

CREATE INDEX ix_map_population_observations_payload_id ON map_population_observations
    (payload_id);

CREATE INDEX ix_map_road_observations_last_attempt_id ON map_road_observations (last_attempt_id);

CREATE INDEX ix_map_road_observations_payload_id ON map_road_observations (payload_id);
"""


class PayloadStorageError(Exception):
    """Safe public error: never expose filenames, provider bodies or credentials."""


@dataclass(frozen=True)
class StoredFile:
    file_ref: str
    sha256: str
    byte_size: int


class PayloadStore:
    def __init__(self, root):
        self.root = Path(root)

    def put_bytes(self, body):
        return self.put_chunks((body,))

    def put_chunks(self, chunks):
        temporary = None
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            digest, size = hashlib.sha256(), 0
            with tempfile.NamedTemporaryFile(dir=self.root, delete=False) as handle:
                temporary = Path(handle.name)
                for body in chunks:
                    handle.write(body)
                    digest.update(body)
                    size += len(body)
                handle.flush()
                os.fsync(handle.fileno())
            sha = digest.hexdigest()
            key = f"sha256/{sha[:2]}/{sha}.blob"
            target = self.root / key
            if not target.resolve().is_relative_to(self.root.resolve()):
                raise PayloadStorageError("원본 보관 파일의 참조가 올바르지 않습니다.")
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(temporary, target)
            except FileExistsError:
                pass
            # Verify an existing object too, without holding a whole archive in memory.
            check, actual_size = hashlib.sha256(), 0
            with target.open("rb") as handle:
                while body := handle.read(1024 * 1024):
                    check.update(body)
                    actual_size += len(body)
            if actual_size != size or check.hexdigest() != sha:
                raise PayloadStorageError("원본 보관 파일의 무결성을 확인할 수 없습니다.")
            for directory in (target.parent, target.parent.parent, self.root):
                fd = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            return StoredFile(key, sha, size)
        except OSError, ValueError:
            raise PayloadStorageError("원본 보관 파일을 저장할 수 없습니다.") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


VERSION = "normalized-2026-11-03.1"
CONGESTION = {
    "여유": "relaxed",
    "보통": "normal",
    "약간 붐빔": "slightly_crowded",
    "붐빔": "crowded",
}


def number(value, *, integer=False):
    if value is None or isinstance(value, bool):
        return None
    try:
        value = Decimal(str(value).strip())
    except InvalidOperation, ValueError:
        return None
    if not value.is_finite() or value < 0 or (integer and value != value.to_integral_value()):
        return None
    return int(value) if integer else value


def timestamp(value):
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=ZoneInfo("Asia/Seoul"))
    if not value or not isinstance(value, str):
        return None
    try:
        return timestamp(datetime.fromisoformat(value.strip()))
    except ValueError:
        return None


def text_value(value):
    return str(value).strip() or None if value is not None else None


def first(value):
    if isinstance(value, list):
        return next((row for row in value if isinstance(row, dict)), {})
    return value if isinstance(value, dict) else {}


def population(data):
    row = first(data.get("LIVE_PPLTN_STTS"))
    if not row:
        return None
    low = number(row.get("AREA_PPLTN_MIN"), integer=True)
    high = number(row.get("AREA_PPLTN_MAX"), integer=True)
    if low is not None and high is not None and low > high:
        low = high = None
    return dict(
        min_people=low,
        max_people=high,
        congestion=CONGESTION.get(row.get("AREA_CONGEST_LVL")),
        is_replacement={"Y": True, "N": False}.get(row.get("REPLACE_YN")),
        observed_at=timestamp(row.get("PPLTN_TIME")),
        reason_code=None if low is not None and high is not None else "INVALID_OR_MISSING_VALUE",
    )


def parking_fields(record):
    d = record.get("details") or {}
    getpark = record["source"] == "seoul_getparkinfo"
    keys = (
        ("PRK_HM", "PRK_CRG", "ADD_UNIT_TM_MNT", "ADD_CRG")
        if getpark
        else ("TIME_RATES", "RATES", "ADD_TIME_RATES", "ADD_RATES")
    )
    fees = dict(
        zip(
            ("base_minutes", "base_fee_krw", "additional_minutes", "additional_fee_krw"),
            (number(d.get(k)) for k in keys),
            strict=True,
        )
    )

    def hours(start, end):
        a, b = text_value(d.get(start)), text_value(d.get(end))
        return f"{a}~{b}" if a is not None and b is not None else None

    return dict(
        provider_record_id=record["source_id"],
        name=record["name"],
        address=record.get("address"),
        registered_capacity=number(
            d.get("TPKCT") if d.get("TPKCT") is not None else d.get("CPCTY"), integer=True
        ),
        **fees,
        daily_max_krw=None,
        fee_notes=None,
        weekday_hours=hours("WD_OPER_BGNG_TM", "WD_OPER_END_TM"),
        saturday_hours=hours("WE_OPER_BGNG_TM", "WE_OPER_END_TM"),
        sunday_holiday_hours=hours("LHLDY_BGNG", "LHLDY"),
        hours_notes=None,
        source_updated_at=record.get("source_updated_at") if getpark else None,
    )


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()


def source_target(key):
    if key.startswith("topis_attempt:"):
        return "topis_traffic", "link:" + key.split(":", 1)[1], "attempt"
    if key.endswith(":attempt"):
        source, target, _ = source_target(key.removesuffix(":attempt"))
        return source, target, "attempt"
    if key.startswith("topis:"):
        return "topis_traffic", "link:" + key.split(":", 1)[1], "data"
    if key.startswith("topis_geometry:"):
        return "topis_geometry", "file:topis_vertices", "data"
    if key.endswith("_status"):
        return (
            "topis_traffic" if key.startswith("topis") else key.removesuffix("_status"),
            "report:" + key,
            "report",
        )
    return key, "all", "data"


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
            old = (
                select(func.coalesce(raw.c.received_at, raw.c.legacy_collected_at, raw.c.stored_at))
                .where(raw.c.payload_id == t.c.payload_id)
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

    def normalize(self, key, data, payload_id, *, received_at=None):
        now = datetime.now(UTC)
        common = dict(
            last_payload_id=payload_id, processed_at=now, processor_version=VERSION, is_active=True
        )
        if key.startswith("topis_geometry:"):
            self.source("topis_geometry")
            self.source("topis_standard_mapping")
            for f in data.get("features", []):
                p = f.get("properties") or {}
                self.upsert(
                    "road_links",
                    dict(
                        link_id=str(f["id"]),
                        name=p.get("name"),
                        direction=p.get("direction"),
                        geometry=func.ST_Multi(
                            func.ST_SetSRID(
                                func.ST_GeomFromGeoJSON(json.dumps(f["geometry"])), 4326
                            )
                        ),
                        source="topis_geometry",
                        source_version=str(p.get("source_year") or key.split(":", 1)[1]),
                        **common,
                    ),
                    ["link_id"],
                )
            for m in data.get("mapping_rows", []):
                if self.session.scalar(
                    select(self.t("road_links").c.link_id).where(
                        self.t("road_links").c.link_id == m["service_link_id"]
                    )
                ):
                    self.upsert(
                        "road_link_mappings",
                        dict(
                            **m,
                            source="topis_standard_mapping",
                            source_version="2025-03-06",
                            last_payload_id=payload_id,
                            processed_at=now,
                            processor_version=VERSION,
                        ),
                        ["service_link_id", "sequence", "standard_link_id"],
                    )
        elif key == "citydata_place_boundaries":
            self.source(key)
            for f in data.get("features", []):
                self.upsert(
                    "population_areas",
                    dict(
                        area_code=str(f["id"]),
                        name=f["properties"]["name"],
                        geometry=func.ST_Multi(
                            func.ST_SetSRID(
                                func.ST_GeomFromGeoJSON(json.dumps(f["geometry"])), 4326
                            )
                        ),
                        source=key,
                        **common,
                    ),
                    ["area_code"],
                )
        elif key in {"sgis_areas", "sgis_seoul_boundary"}:
            self.source(key)
            version = fingerprint(data.get("features", []))
            for f in data.get("features", []):
                p = f.get("properties") or {}
                shape = func.ST_Multi(
                    func.ST_SetSRID(func.ST_GeomFromGeoJSON(json.dumps(f["geometry"])), 4326)
                )
                if key == "sgis_areas":
                    parent = p.get("sigungu_code")
                    if not parent:
                        return "missing_sgis_parent_code"
                    values = dict(
                        dong_code=str(f["id"]),
                        name=p["name"],
                        sigungu_code=str(parent),
                        code_system="sgis",
                        geometry=shape,
                        source=key,
                        boundary_version=version,
                        **common,
                    )
                    self.upsert("administrative_areas", values, ["dong_code"])
                else:
                    self.upsert(
                        "region_boundaries",
                        dict(
                            code_system="sgis",
                            region_code="11",
                            name="서울특별시",
                            geometry=shape,
                            source=key,
                            source_version=str(data.get("boundary_year"))
                            if data.get("boundary_year")
                            else None,
                            boundary_version=version,
                            **common,
                        ),
                        ["code_system", "region_code"],
                    )
        elif key.startswith("topis:"):
            link = key.split(":", 1)[1]
            t = self.t("road_links")
            if not self.session.scalar(select(t.c.link_id).where(t.c.link_id == link)):
                return "missing_road_geometry"
            rows = [r for r in data.get("rows", []) if str(r.get("link_id")) == link]
            if len(rows) != 1:
                return "invalid_link_response"
            self.source("topis_traffic")
            self.upsert(
                "road_traffic_latest",
                dict(
                    source="topis_traffic",
                    link_id=link,
                    payload_id=payload_id,
                    speed_kmh=number(rows[0].get("prcs_spd")),
                    travel_seconds=number(rows[0].get("prcs_trv_time")),
                    observed_at=None,
                    processed_at=now,
                    processor_version=VERSION,
                    reason_code="SOURCE_TIME_UNKNOWN",
                ),
                ["source", "link_id"],
                newer_payload=True,
            )
        elif key == "seoul_citydata":
            area = str(data.get("AREA_CD") or "")
            t = self.t("population_areas")
            if not self.session.scalar(select(t.c.area_code).where(t.c.area_code == area)):
                return "missing_population_geometry"
            values = population(data)
            if values:
                self.source(key)
                self.upsert(
                    "population_latest",
                    dict(
                        source=key,
                        area_code=area,
                        payload_id=payload_id,
                        processed_at=now,
                        processor_version=VERSION,
                        **values,
                    ),
                    ["source", "area_code"],
                    newer_payload=True,
                )
        return None


def official_mapping(*, preserve_sequence=False):
    path = Path(__file__).resolve().parents[2] / "jobs/data/topis_standard_mapping_20250306.xlsx"
    if (
        hashlib.sha256(path.read_bytes()).hexdigest()
        != "3ecabd782ac7ebec64fed32e4f61c792de70b38594c9e966736d283ceea3baad"
    ):
        raise RuntimeError("0009 official mapping evidence changed")
    ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    services = {}
    with zipfile.ZipFile(path) as book:
        strings = [
            "".join(si.itertext()) for si in ET.fromstring(book.read("xl/sharedStrings.xml"))
        ]
        sheet = ET.fromstring(book.read("xl/worksheets/sheet1.xml"))
        for row in sheet.findall(".//s:row", ns)[1:]:
            values = {}
            for cell in row.findall("s:c", ns):
                node = cell.find("s:v", ns)
                if node is not None:
                    values[re.sub(r"\d+", "", cell.get("r"))] = (
                        strings[int(node.text)] if cell.get("t") == "s" else node.text
                    )
            if not values:
                continue
            service, standard = values["A"], values["C"]
            seq = int(values.get("B") or 0)
            if (
                not re.fullmatch(r"\d{10}", service)
                or not re.fullmatch(r"\d{10}", standard)
                or seq < 0
            ):
                raise ValueError("invalid_official_mapping")
            services.setdefault(service, []).append((seq, standard))
    if preserve_sequence:
        return [
            {"service_link_id": service, "sequence": seq, "standard_link_id": standard}
            for service, pairs in services.items()
            for seq, standard in sorted(set(pairs))
        ]
    return {k: [standard for _, standard in sorted(set(v))] for k, v in services.items()}


def backfill(connection, store, legacy_tables):
    metadata = sa.MetaData()
    metadata.reflect(connection, only=TABLES)
    session = Session(bind=connection)
    writer = NormalizedWriter(session, metadata)
    now = datetime.now(UTC)

    def geometry(value):
        return func.ST_GeomFromEWKB(bytes.fromhex(value) if isinstance(value, str) else value)

    def old(name):
        if name not in legacy_tables:
            return []
        # PostgreSQL text preserves JSON numeric precision before Python decoding.
        columns = {
            "external_api_responses": "payload",
            "external_api_observations": "data",
            "provider_snapshots": "data",
            "citydata_snapshots": "data",
            "parking_records": "details",
            "parking_source_records": "registered",
            "map_observations": "value",
        }
        field = columns.get(name)
        extra = f', "{field}"::text AS original_json' if field else ""
        return list(
            connection.execute(sa.text(f'SELECT *{extra} FROM legacy_v0008."{name}"')).mappings()
        )

    def insert_row(table, **values):
        return session.execute(sa.insert(metadata.tables[table]).values(**values))

    run_by_id = {}
    raw_by_id = {}

    def new_run(source, target, started, *, run_id=None, **extra):
        writer.source(source)
        key = run_id or uuid4()
        reasons = extra.pop("rejection_reasons", {})
        if not isinstance(reasons, dict):
            raise RuntimeError("0009 invalid legacy rejection reasons")
        insert_row(
            "collection_runs",
            id=key,
            source=source,
            target_key=target,
            started_at=started,
            run_kind="legacy",
            status=extra.pop("status", "success"),
            **extra,
        )
        for reason, count in reasons.items():
            if (
                not isinstance(reason, str)
                or not reason.strip()
                or isinstance(count, bool)
                or not isinstance(count, int)
                or count < 0
            ):
                raise RuntimeError("0009 invalid legacy rejection count")
            if count:
                insert_row(
                    "collection_rejection_reasons",
                    run_id=key,
                    reason_code=reason,
                    occurrence_count=count,
                )
        run_by_id[key] = (source, target)
        return key

    def archive(
        source,
        target,
        body,
        table,
        key,
        stamp,
        *,
        run_id=None,
        payload_id=None,
        kind="legacy_snapshot",
        parent=None,
        basis="snapshot_saved",
        original_json=None,
    ):
        rid = run_id or new_run(source, target, stamp or now)
        pid = payload_id or uuid4()
        values = dict(
            payload_id=pid,
            run_id=rid,
            part_key=f"legacy-response:{key}"
            if table == "external_api_responses"
            else f"{table}:{key}",
            format="json",
            payload_kind=kind,
            legacy_table=table,
            legacy_key=str(key)
            + (
                "@" + stamp.isoformat()
                if table in {"provider_snapshots", "citydata_snapshots"} and stamp
                else ""
            ),
            parent_payload_id=parent,
        )
        content = (
            original_json
            if original_json is not None
            else json.dumps(body, ensure_ascii=False, default=str, allow_nan=False)
        )
        decoded = json.loads(content)
        saved = store.put_bytes(content.encode("utf-8"))
        values.update(
            file_ref=saved.file_ref,
            sha256=saved.sha256,
            byte_size=saved.byte_size,
            document_type="object"
            if isinstance(decoded, dict)
            else "array"
            if isinstance(decoded, list)
            else "scalar",
        )
        if stamp is not None:
            values.update(legacy_collected_at=stamp, legacy_time_basis=basis)
        insert_row("raw_payloads", **values)
        raw_by_id[pid] = rid
        return pid

    def normalize(key, body, pid):
        try:
            with session.begin_nested():
                issue = writer.normalize(key, body, pid)
        except Exception:
            issue = "legacy_normalization_requires_review"
        if issue:
            # Preserve the evidence and make incomplete conversion visible to operators.
            t = metadata.tables["collection_runs"]
            session.execute(
                sa.update(t)
                .where(t.c.id == raw_by_id[pid])
                .values(
                    status="partial",
                    completeness="unknown",
                    rejected=t.c.rejected + 1,
                )
            )
            reasons = metadata.tables["collection_rejection_reasons"]
            session.execute(
                insert(reasons)
                .values(run_id=raw_by_id[pid], reason_code=issue, occurrence_count=1)
                .on_conflict_do_update(
                    index_elements=["run_id", "reason_code"],
                    set_={"occurrence_count": reasons.c.occurrence_count + 1},
                )
            )
        return issue

    for row in old("collection_runs"):
        code, target, _ = source_target(row["source"])
        if row["area_code"]:
            target = "area:" + row["area_code"]
        elif row["area_name"]:
            target = "area_name:" + row["area_name"]
        values = {
            k: row[k]
            for k in (
                "status",
                "finished_at",
                "received",
                "accepted",
                "rejected",
                "duplicates",
                "error_code",
                "rejection_reasons",
            )
        }
        new_run(code, target, row["started_at"], run_id=row["id"], **values)

    for row in old("external_api_responses"):
        code, target, _ = source_target(row["source"])
        archive(
            code,
            target,
            row["payload"],
            "external_api_responses",
            row["id"],
            row["collected_at"],
            run_id=row["run_id"],
            payload_id=row["id"],
            original_json=row["original_json"],
            basis="run_started",
        )
    for row in old("external_api_observations"):
        parent = row["response_id"]
        source, target = run_by_id[raw_by_id[parent]]
        archive(
            source,
            target,
            row["data"],
            "external_api_observations",
            row["id"],
            None,
            run_id=raw_by_id[parent],
            kind="legacy_observation",
            parent=parent,
            original_json=row["original_json"],
        )

    providers = old("provider_snapshots")
    snapshots = []
    # First construct the canonical geometry parents, then their observations.
    for row in sorted(
        providers,
        key=lambda r: (
            not (
                "geometry" in r["source"] or "boundar" in r["source"] or r["source"] == "sgis_areas"
            ),
            r["collected_at"],
        ),
    ):
        source, target, kind = source_target(row["source"])
        data = row["data"]
        attempt_run = None
        if kind == "attempt":
            attempt_run = new_run(
                source,
                target,
                row["collected_at"],
                status=data.get("status", "failed"),
                error_code=data.get("error_code"),
                finished_at=row["collected_at"],
            )
            # Attempt payload itself remains queryable as evidence too.
        pid = archive(
            source,
            target,
            data,
            "provider_snapshots",
            row["source"],
            row["collected_at"],
            run_id=attempt_run,
            kind="processing_report" if kind in {"attempt", "report"} else "legacy_snapshot",
            original_json=row["original_json"],
        )
        if kind == "data":
            if row["source"].startswith("topis_geometry:") and "mapping_rows" not in data:
                data = dict(data, mapping_rows=official_mapping(preserve_sequence=True))
            snapshots.append((row["source"], data, pid))
            if not row["source"].startswith("topis:"):
                normalize(row["source"], data, pid)

    old_features = old("map_features")
    feature_by_id = {(r["layer"], r["feature_id"]): r for r in old_features}
    writer.source("legacy_map_geometry", "전환 전 지도 도형 (원본 출처 미확인)")
    # Published geometry is useful evidence when its original snapshot is unavailable.
    for row in old_features:
        layer, key = row["layer"], row["feature_id"]
        if layer not in {"roads", "population"}:
            continue
        table, idcol = (
            ("road_links", "link_id") if layer == "roads" else ("population_areas", "area_code")
        )
        t = metadata.tables[table]
        if session.scalar(select(t.c[idcol]).where(t.c[idcol] == key)):
            continue
        props = row["properties"] or {}
        # Invalid legacy geometry must not be invented or silently exposed as current.
        try:
            with session.begin_nested():
                writer.upsert(
                    table,
                    {
                        idcol: key,
                        "name": props.get("name"),
                        "geometry": func.ST_Multi(geometry(row["geometry"])),
                        "source": "legacy_map_geometry",
                        "is_active": row["is_active"],
                    },
                    [idcol],
                )
        except Exception:
            pass  # Retained in the verified legacy archive; its layer stays unready below.

    # Preserve additional master metadata where a real polygon is available.
    for row in old("population_areas"):
        t = metadata.tables["population_areas"]
        session.execute(
            sa.update(t)
            .where(t.c.area_code == row["area_code"])
            .values(latitude=row["latitude"], longitude=row["longitude"], district=row["district"])
        )

    for row in old("citydata_snapshots"):
        pid = archive(
            "seoul_citydata",
            "area:" + row["area_code"],
            row["data"],
            "citydata_snapshots",
            row["area_code"],
            row["collected_at"],
            original_json=row["original_json"],
            basis="run_started",
        )
        normalize("seoul_citydata", row["data"], pid)
    for key, data, pid in snapshots:
        if key.startswith("topis:"):
            normalize(key, data, pid)

    legacy_records = {r["id"]: r for r in old("parking_records")}
    source_rows = old("parking_source_records")
    linked = {r["legacy_record_id"]: r for r in source_rows if r["legacy_record_id"]}
    facilities = {r["id"]: r for r in old("parking_facilities")}
    written_facilities = set()

    def facility(pid, record=None):
        if pid in written_facilities:
            return
        f = feature_by_id.get(("parking", str(pid)))
        location = (
            geometry(f["geometry"]) if f else geometry(record["location"]) if record else None
        )
        name = (f["properties"] or {}).get("name") if f else record["name"] if record else None
        if not name or location is None:
            raise RuntimeError(
                f"Parking identity {pid} lacks name/point; repair legacy evidence before 0009"
            )
        original = facilities.get(pid, {})
        insert_row(
            "parking_facilities",
            parking_id=pid,
            name=name,
            location=location,
            identity_status=original.get("identity_status", "unverified"),
            is_active=original.get("is_active", True),
        )
        written_facilities.add(pid)

    for record in legacy_records.values():
        previous = linked.get(record["id"])
        pid = previous["parking_id"] if previous else uuid4()
        rid = previous["id"] if previous else uuid4()
        facility(pid, record)
        writer.source(record["source"])
        raw = archive(
            record["source"],
            "all",
            record["details"],
            "parking_records",
            record["id"],
            record["collected_at"],
            original_json=record["original_json"],
            basis="run_started",
        )
        insert_row(
            "parking_source_records",
            source_record_id=rid,
            parking_id=pid,
            legacy_record_id=record["id"],
            source=record["source"],
            location=geometry(record["location"]),
            last_payload_id=raw,
            collected_at=None,
            processed_at=now,
            processor_version=VERSION,
            is_active=previous["is_active"] if previous else True,
            reason_code="SOURCE_TIME_UNKNOWN",
            **parking_fields(record),
        )

    for row in source_rows:
        if row["legacy_record_id"] in legacy_records:
            continue
        facility(row["parking_id"])
        writer.source(row["source"])
        body = row["registered"] or {}
        value = body.get("value") or {}
        fees, hours = value.get("fees") or {}, value.get("operating_hours") or {}
        raw = archive(
            row["source"],
            "all",
            body,
            "parking_source_records",
            row["id"],
            timestamp(body.get("collected_at")),
            original_json=row["original_json"],
        )
        try:
            ref = date.fromisoformat(row["reference_date"]) if row["reference_date"] else None
        except ValueError:
            ref = None
        insert_row(
            "parking_source_records",
            source_record_id=row["id"],
            parking_id=row["parking_id"],
            legacy_record_id=row["legacy_record_id"],
            source=row["source"],
            reference_date=ref,
            last_payload_id=raw,
            address=value.get("address"),
            registered_capacity=number(value.get("registered_capacity"), integer=True),
            **{
                k: number(fees.get(k))
                for k in (
                    "base_minutes",
                    "base_fee_krw",
                    "additional_minutes",
                    "additional_fee_krw",
                    "daily_max_krw",
                )
            },
            fee_notes=fees.get("notes"),
            weekday_hours=hours.get("weekdays"),
            saturday_hours=hours.get("saturday"),
            sunday_holiday_hours=hours.get("sunday_holidays"),
            hours_notes=hours.get("notes"),
            source_updated_at=timestamp(body.get("observed_at")),
            is_active=row["is_active"],
            reason_code=body.get("reason_code"),
        )

    # Preserve public geometry identities. A new version prevents stale client geometry caches.
    for row in old("map_layer_versions"):
        layer = row["layer"]
        if layer not in {"roads", "parking", "population"}:
            continue
        insert_row(
            "map_layer_versions",
            layer=layer,
            geometry_version="normalized:" + (row["geometry_version"] or "legacy"),
            is_ready=False,
            coverage=row["coverage"] or "Legacy coverage",
        )
    for row in old_features:
        layer, key = row["layer"], row["feature_id"]
        if layer not in {"roads", "parking", "population"}:
            continue
        props = row["properties"] or {}
        try:
            with session.begin_nested():
                extra = (
                    {"road_link_id": key, "direction": props.get("direction")}
                    if layer == "roads"
                    else {"population_area_code": key}
                    if layer == "population"
                    else {
                        "parking_id": UUID(key),
                        "identity_status": facilities.get(UUID(key), {}).get(
                            "identity_status", "unverified"
                        ),
                    }
                )
                shape = geometry(row["geometry"])
                insert_row(
                    "map_features",
                    layer=layer,
                    feature_id=key,
                    name=props.get("name"),
                    geometry=shape if layer == "parking" else func.ST_Multi(shape),
                    is_active=row["is_active"],
                    **extra,
                )
        except Exception:
            continue
    for row in old("map_observations"):
        layer, key = row["layer"], row["feature_id"]
        if layer not in {"roads", "population"}:
            continue
        feature = metadata.tables["map_features"]
        if not session.scalar(
            select(feature.c.feature_id).where(
                feature.c.layer == layer, feature.c.feature_id == key
            )
        ):
            continue
        source = "legacy_map_publication"
        value = row["value"]
        raw = archive(
            source,
            ("link:" if layer == "roads" else "area:") + key,
            value if isinstance(value, dict) else {"value": value},
            "map_observations",
            layer + ":" + key,
            row["collected_at"],
            kind="legacy_observation",
            basis="unknown",
            original_json=row["original_json"],
        )
        has = isinstance(value, dict)
        fields = {}
        if has:
            if layer == "roads":
                fields = {
                    "speed_kmh": number(value.get("speed_kmh")),
                    "travel_seconds": number(value.get("travel_seconds")),
                }
            else:
                low, high = (
                    number(value.get("min_people"), integer=True),
                    number(value.get("max_people"), integer=True),
                )
                if low is not None and high is not None and low > high:
                    low = high = None
                fields = dict(
                    min_people=low,
                    max_people=high,
                    congestion=value.get("congestion")
                    if value.get("congestion") in CONGESTION.values()
                    else None,
                    is_replacement=value.get("is_replacement")
                    if isinstance(value.get("is_replacement"), bool)
                    else None,
                )
            fields.update(
                source=source,
                payload_id=raw,
                observed_at=row["observed_at"],
                processed_at=now,
                processor_version=VERSION,
            )
        if has:
            table = metadata.tables[
                "road_traffic_latest" if layer == "roads" else "population_latest"
            ]
            idcol = "link_id" if layer == "roads" else "area_code"
            if not session.scalar(select(table.c[idcol]).where(table.c[idcol] == key).limit(1)):
                insert_row(table.name, **{idcol: key}, **fields)
        insert_row(
            "map_road_observations" if layer == "roads" else "map_population_observations",
            **{"link_id" if layer == "roads" else "area_code": key},
            layer=layer,
            has_observation=has,
            collection_failed=row["collection_failed"],
            **fields,
        )
    for layer in ("roads", "parking", "population"):
        expected = sum(r["layer"] == layer for r in old_features)
        actual = session.scalar(
            select(func.count())
            .select_from(metadata.tables["map_features"])
            .where(metadata.tables["map_features"].c.layer == layer)
        )
        previous = next((r for r in old("map_layer_versions") if r["layer"] == layer), None)
        if previous and expected and actual == expected and previous["is_ready"]:
            t = metadata.tables["map_layer_versions"]
            session.execute(sa.update(t).where(t.c.layer == layer).values(is_ready=True))
    session.flush()


def archive_legacy(connection, store, legacy):
    quote = connection.dialect.identifier_preparer.quote
    for table in legacy.sorted_tables:
        qualified = f"{quote(table.schema)}.{quote(table.name)}"
        connection.exec_driver_sql(f"LOCK TABLE {qualified} IN ACCESS EXCLUSIVE MODE")
        columns = [
            dict(name=c.name, type=str(c.type.compile(dialect=connection.dialect)))
            for c in table.columns
        ]
        header = dict(
            schema=table.schema,
            table=table.name,
            source_revision="0008",
            encoding="postgresql-text-cells",
            columns=columns,
            ddl=str(CreateTable(table).compile(dialect=connection.dialect)),
            indexes=[
                str(CreateIndex(i).compile(dialect=connection.dialect))
                for i in sorted(table.indexes, key=lambda i: i.name)
            ],
        )
        projection = ",".join(f"{quote(c.name)}::text" for c in table.columns)
        count = 0

        def chunks(header=header, projection=projection, qualified=qualified):
            nonlocal count
            yield json.dumps(header, ensure_ascii=False).encode("utf-8") + b"\n"
            result = connection.exec_driver_sql(
                f"SELECT {projection} FROM {qualified}",
                execution_options={"stream_results": True},
            )
            for row in result:
                yield json.dumps(list(row), ensure_ascii=False).encode("utf-8") + b"\n"
                count += 1

        saved = store.put_chunks(chunks())
        if connection.exec_driver_sql(f"SELECT count(*) FROM {qualified}").scalar_one() != count:
            raise RuntimeError("0009 legacy export row count mismatch")
        connection.execute(
            sa.text("""INSERT INTO legacy_archives
            (schema_name,table_name,source_revision,file_ref,sha256,byte_size,row_count)
            VALUES(:schema,:table,'0008',:ref,:sha,:size,:count)"""),
            dict(
                schema=table.schema,
                table=table.name,
                ref=saved.file_ref,
                sha=saved.sha256,
                size=saved.byte_size,
                count=count,
            ),
        )
    for table in reversed(legacy.sorted_tables):
        connection.exec_driver_sql(f"DROP TABLE {quote(table.schema)}.{quote(table.name)} RESTRICT")
    connection.exec_driver_sql("DROP SCHEMA legacy_v0008 RESTRICT")


def upgrade():
    if context.is_offline_mode():
        raise RuntimeError("0009 requires online data conversion and verified file export")
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    present = set(inspector.get_table_names(schema="public")) & set(LEGACY_TABLES)
    required = set(LEGACY_TABLES) - {
        "external_api_responses",
        "external_api_observations",
        "map_layer_versions",
        "map_features",
        "map_observations",
        "parking_facilities",
        "parking_source_records",
    }
    if not required <= present:
        raise RuntimeError("0009 missing baseline tables; inspect the existing 0008 schema")
    for group in [
        {"external_api_responses", "external_api_observations"},
        {
            "map_layer_versions",
            "map_features",
            "map_observations",
            "parking_facilities",
            "parking_source_records",
        },
    ]:
        if present & group and not group <= present:
            raise RuntimeError("0009 incomplete legacy branch schema; repair before upgrading")
    if "legacy_v0008" in inspector.get_schema_names():
        raise RuntimeError("0009 temporary legacy schema already exists; inspect before upgrading")
    store = PayloadStore(context.config.attributes["payload_storage_dir"])
    op.execute("CREATE SCHEMA legacy_v0008")
    for name in sorted(present):
        connection.exec_driver_sql(f'LOCK TABLE public."{name}" IN ACCESS EXCLUSIVE MODE')
        op.execute(f'ALTER TABLE public."{name}" SET SCHEMA legacy_v0008')
    for statement in SCHEMA_SQL.split(";"):
        if statement.strip():
            op.execute(statement)
    backfill(connection, store, present)
    legacy = sa.MetaData()
    legacy.reflect(connection, schema="legacy_v0008", only=sorted(present))
    archive_legacy(connection, store, legacy)


def downgrade():
    raise RuntimeError(
        "0009 downgrade requires restoring the DB and evidence-file backup together; "
        "automatic data loss is blocked"
    )
