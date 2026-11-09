"""Persist historical recommendation facts, ranking decisions and search coverage.

Revision ID: 0013
Revises: 0012
DDL is frozen here, independent of runtime models.
Only new tables are added. Nonempty result context cannot be downgraded.
"""

import sqlalchemy as sa
from alembic import context, op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE recommendation_results (
        result_id UUID NOT NULL,
        trip_id UUID NOT NULL,
        condition_version INTEGER NOT NULL,
        request_id UUID NOT NULL,
        kind TEXT NOT NULL,
        source_result_id UUID,
        routes_status TEXT NOT NULL,
        explanation_status TEXT NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE NOT NULL,
        context_expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
        context_token_digest VARCHAR(64) NOT NULL,
        ranking_policy TEXT,
        time_bucket_seconds INTEGER,
        parking_bonus_radius_m INTEGER,
        traffic_max_age_seconds INTEGER,
        search_fallback BOOLEAN,
        search_more_available BOOLEAN,
        CONSTRAINT pk_recommendation_results PRIMARY KEY (result_id),
        CONSTRAINT fk_recommendation_results_trip_id_chat_condition_versions FOREIGN KEY(trip_id,
        condition_version) REFERENCES chat_condition_versions (trip_id, version) ON DELETE
        RESTRICT,
        CONSTRAINT uq_recommendation_results_trip_id UNIQUE (trip_id, result_id),
        CONSTRAINT fk_recommendation_results_retry_source FOREIGN KEY(trip_id, source_result_id)
        REFERENCES recommendation_results (trip_id, result_id) ON DELETE RESTRICT,
        CONSTRAINT uq_recommendation_results_result_id UNIQUE (result_id, kind),
        CONSTRAINT ck_recommendation_results_condition_version CHECK (condition_version >= 0),
        CONSTRAINT ck_recommendation_results_retry_not_self CHECK (source_result_id <> result_id),
        CONSTRAINT ck_recommendation_results_result_kind CHECK (kind IN
        ('recommendation','general_candidates')),
        CONSTRAINT ck_recommendation_results_result_stages CHECK ((kind = 'recommendation' AND
        routes_status IN ('ok','partial') AND explanation_status IN ('ok','fallback')) OR (kind =
        'general_candidates' AND routes_status = 'failed' AND explanation_status = 'skipped')),
        CONSTRAINT ck_recommendation_results_context_expiry CHECK (context_expires_at >
        created_at),
        CONSTRAINT ck_recommendation_results_context_digest CHECK (context_token_digest ~
        '^[0-9a-f]{64}$'),
        CONSTRAINT ck_recommendation_results_ranking_policy_fields CHECK
        (num_nonnulls(ranking_policy, time_bucket_seconds, parking_bonus_radius_m,
        traffic_max_age_seconds) IN (0,4)),
        CONSTRAINT ck_recommendation_results_ranking_policy_nonblank CHECK (btrim(ranking_policy)
        <> ''),
        CONSTRAINT ck_recommendation_results_time_bucket CHECK (time_bucket_seconds > 0),
        CONSTRAINT ck_recommendation_results_parking_bonus_radius CHECK (parking_bonus_radius_m >=
        0),
        CONSTRAINT ck_recommendation_results_traffic_max_age CHECK (traffic_max_age_seconds > 0),
        CONSTRAINT ck_recommendation_results_search_scope_fields CHECK
        (num_nonnulls(search_fallback, search_more_available) IN (0,2)),
        CONSTRAINT uq_recommendation_results_context_token_digest UNIQUE (context_token_digest)
        )
    """)
    op.execute("""
        CREATE INDEX ix_recommendation_results_context_expires_at ON recommendation_results
        (context_expires_at)
    """)
    op.execute("""
        CREATE INDEX ix_recommendation_results_trip_created ON recommendation_results (trip_id,
        created_at)
    """)
    op.execute("""
        CREATE INDEX ix_recommendation_results_trip_request ON recommendation_results (trip_id,
        request_id)
    """)
    op.execute("""
        CREATE TABLE recommendation_cards (
        result_id UUID NOT NULL,
        place_id TEXT NOT NULL,
        result_kind TEXT NOT NULL,
        position INTEGER NOT NULL,
        rank INTEGER,
        place_name TEXT NOT NULL,
        purpose TEXT,
        category_code TEXT,
        category_name TEXT,
        place_location geography(POINT,4326) NOT NULL,
        address TEXT,
        road_address TEXT,
        phone TEXT,
        provider_url TEXT,
        place_freshness TEXT NOT NULL,
        place_observed_at TIMESTAMP WITH TIME ZONE,
        place_collected_at TIMESTAMP WITH TIME ZONE,
        place_source TEXT,
        place_reason_code TEXT,
        duration_seconds INTEGER,
        distance_meters NUMERIC,
        route_status TEXT NOT NULL,
        route_freshness TEXT NOT NULL,
        route_observed_at TIMESTAMP WITH TIME ZONE,
        route_collected_at TIMESTAMP WITH TIME ZONE,
        route_source TEXT,
        route_reason_code TEXT,
        parking_status TEXT NOT NULL,
        parking_reason_code TEXT,
        parking_checked_at TIMESTAMP WITH TIME ZONE NOT NULL,
        parking_search_radius_meters INTEGER,
        parking_nearest_distance_meters NUMERIC,
        parking_total_count INTEGER,
        reason_text TEXT NOT NULL,
        reason_source TEXT NOT NULL,
        ranking_evaluated_at TIMESTAMP WITH TIME ZONE,
        ranking_eta_bucket INTEGER,
        ranking_traffic_applied BOOLEAN,
        ranking_parking_adjustment INTEGER,
        ranking_adjusted_grade TEXT,
        CONSTRAINT pk_recommendation_cards PRIMARY KEY (result_id, place_id),
        CONSTRAINT fk_recommendation_cards_result_id_recommendation_results FOREIGN KEY(result_id,
        result_kind) REFERENCES recommendation_results (result_id, kind) ON DELETE CASCADE,
        CONSTRAINT uq_recommendation_cards_position UNIQUE (result_id, position),
        CONSTRAINT uq_recommendation_cards_kind UNIQUE (result_id, place_id, result_kind),
        CONSTRAINT ck_recommendation_cards_card_position CHECK (position BETWEEN 0 AND 2),
        CONSTRAINT ck_recommendation_cards_place_identity CHECK (place_id ~
        '^kakao:[^[:space:]]+$'),
        CONSTRAINT ck_recommendation_cards_place_name_nonblank CHECK (btrim(place_name) <> ''),
        CONSTRAINT ck_recommendation_cards_reason_text_nonblank CHECK (btrim(reason_text) <> ''),
        CONSTRAINT ck_recommendation_cards_place_location CHECK (NOT
        ST_IsEmpty(place_location::geometry)),
        CONSTRAINT ck_recommendation_cards_place_purpose CHECK (purpose IN
        ('cafe','food','walk','exhibition')),
        CONSTRAINT ck_recommendation_cards_place_freshness CHECK (place_freshness IN
        ('fresh','stale','unknown','not_applicable')),
        CONSTRAINT ck_recommendation_cards_route_status CHECK (route_status IN
        ('available','unavailable','unsupported','error')),
        CONSTRAINT ck_recommendation_cards_route_freshness CHECK (route_freshness IN
        ('fresh','stale','unknown','not_applicable')),
        CONSTRAINT ck_recommendation_cards_card_kind_facts CHECK ((result_kind = 'recommendation'
        AND rank IS NOT NULL AND rank = position + 1 AND route_status = 'available' AND
        duration_seconds IS NOT NULL AND distance_meters IS NOT NULL AND reason_source IN
        ('ai','fallback')) OR (result_kind = 'general_candidates' AND rank IS NULL AND
        route_status IN ('unavailable','error') AND duration_seconds IS NULL AND distance_meters
        IS NULL AND reason_source = 'none')),
        CONSTRAINT ck_recommendation_cards_duration_nonnegative CHECK (duration_seconds >= 0),
        CONSTRAINT ck_recommendation_cards_distance_meters_finite CHECK (distance_meters >= 0 AND
        distance_meters < 'Infinity'::numeric),
        CONSTRAINT ck_recommendation_cards_parking_nearest_distance_meters_finite CHECK
        (parking_nearest_distance_meters >= 0 AND parking_nearest_distance_meters <
        'Infinity'::numeric),
        CONSTRAINT ck_recommendation_cards_parking_status CHECK (parking_status IN
        ('available','unavailable','error')),
        CONSTRAINT ck_recommendation_cards_parking_radius CHECK (parking_search_radius_meters IN
        (300,500,1000)),
        CONSTRAINT ck_recommendation_cards_parking_count CHECK (parking_total_count >= 0),
        CONSTRAINT ck_recommendation_cards_available_parking CHECK (parking_status <> 'available'
        OR (parking_search_radius_meters IS NOT NULL AND parking_total_count IS NOT NULL AND
        parking_total_count > 0)),
        CONSTRAINT ck_recommendation_cards_ranking_decision CHECK
        ((num_nonnulls(ranking_evaluated_at, ranking_eta_bucket, ranking_traffic_applied,
        ranking_parking_adjustment, ranking_adjusted_grade) = 0) OR (result_kind =
        'recommendation' AND ranking_evaluated_at IS NOT NULL AND ranking_eta_bucket IS NOT NULL
        AND ranking_traffic_applied IS NOT NULL AND ranking_parking_adjustment IS NOT NULL AND
        ((ranking_traffic_applied AND ranking_adjusted_grade IS NOT NULL) OR (NOT
        ranking_traffic_applied AND ranking_adjusted_grade IS NULL AND ranking_parking_adjustment
        = 0)))),
        CONSTRAINT ck_recommendation_cards_ranking_bucket CHECK (ranking_eta_bucket >= 0),
        CONSTRAINT ck_recommendation_cards_ranking_adjustment CHECK (ranking_parking_adjustment IN
        (0,1)),
        CONSTRAINT ck_recommendation_cards_ranking_grade CHECK (ranking_adjusted_grade IN
        ('원활','서행','정체'))
        )
    """)
    op.execute("""
        CREATE TABLE recommendation_search_areas (
        result_id UUID NOT NULL,
        position INTEGER NOT NULL,
        area_id TEXT NOT NULL,
        name TEXT NOT NULL,
        center geography(POINT,4326) NOT NULL,
        source TEXT NOT NULL,
        geometry_version TEXT NOT NULL,
        congestion TEXT NOT NULL,
        road_observed_at TIMESTAMP WITH TIME ZONE,
        registered_parking_count INTEGER,
        parking_checked_at TIMESTAMP WITH TIME ZONE,
        CONSTRAINT pk_recommendation_search_areas PRIMARY KEY (result_id, position),
        CONSTRAINT fk_recommendation_search_areas_result_id_recommendation_results FOREIGN
        KEY(result_id) REFERENCES recommendation_results (result_id) ON DELETE CASCADE,
        CONSTRAINT uq_recommendation_search_areas_result_id UNIQUE (result_id, area_id),
        CONSTRAINT ck_recommendation_search_areas_area_position CHECK (position >= 0),
        CONSTRAINT ck_recommendation_search_areas_area_id_nonblank CHECK (btrim(area_id) <> ''),
        CONSTRAINT ck_recommendation_search_areas_name_nonblank CHECK (btrim(name) <> ''),
        CONSTRAINT ck_recommendation_search_areas_source_nonblank CHECK (btrim(source) <> ''),
        CONSTRAINT ck_recommendation_search_areas_geometry_version_nonblank CHECK
        (btrim(geometry_version) <> ''),
        CONSTRAINT ck_recommendation_search_areas_center_nonempty CHECK (NOT
        ST_IsEmpty(center::geometry)),
        CONSTRAINT ck_recommendation_search_areas_congestion CHECK (congestion IN
        ('원활','서행','정체','확인 불가')),
        CONSTRAINT ck_recommendation_search_areas_registered_parking_count CHECK
        (registered_parking_count >= 0)
        )
    """)
    op.execute("""
        CREATE TABLE recommendation_evidence (
        result_id UUID NOT NULL,
        place_id TEXT NOT NULL,
        position INTEGER NOT NULL,
        result_kind TEXT NOT NULL,
        type TEXT NOT NULL,
        text TEXT NOT NULL,
        status TEXT DEFAULT 'available' NOT NULL,
        duration_seconds INTEGER,
        distance_meters NUMERIC,
        traffic_grade TEXT,
        freshness TEXT NOT NULL,
        observed_at TIMESTAMP WITH TIME ZONE,
        collected_at TIMESTAMP WITH TIME ZONE,
        source TEXT,
        reason_code TEXT,
        CONSTRAINT pk_recommendation_evidence PRIMARY KEY (result_id, place_id, position),
        CONSTRAINT fk_recommendation_evidence_result_id_recommendation_cards FOREIGN
        KEY(result_id, place_id, result_kind) REFERENCES recommendation_cards (result_id,
        place_id, result_kind) ON DELETE CASCADE,
        CONSTRAINT ck_recommendation_evidence_recommended_evidence CHECK (result_kind =
        'recommendation'),
        CONSTRAINT ck_recommendation_evidence_evidence_position CHECK (position >= 0),
        CONSTRAINT ck_recommendation_evidence_text_nonblank CHECK (btrim(text) <> ''),
        CONSTRAINT ck_recommendation_evidence_evidence_value CHECK ((type = 'eta' AND status =
        'available' AND duration_seconds IS NOT NULL AND distance_meters IS NOT NULL AND
        traffic_grade IS NULL) OR (type = 'parking' AND status = 'available' AND duration_seconds
        IS NULL AND distance_meters IS NOT NULL AND traffic_grade IS NULL) OR (type = 'traffic'
        AND duration_seconds IS NULL AND distance_meters IS NULL AND ((status = 'available' AND
        traffic_grade IS NOT NULL AND source IS NOT NULL AND btrim(source) <> '' AND observed_at
        IS NOT NULL) OR (status IN ('unavailable','unsupported','error') AND traffic_grade IS NULL
        AND reason_code IS NOT NULL AND btrim(reason_code) <> '')))),
        CONSTRAINT ck_recommendation_evidence_traffic_grade CHECK (traffic_grade IN
        ('원활','서행','정체')),
        CONSTRAINT ck_recommendation_evidence_duration_nonnegative CHECK (duration_seconds >= 0),
        CONSTRAINT ck_recommendation_evidence_distance_meters_finite CHECK (distance_meters >= 0
        AND distance_meters < 'Infinity'::numeric),
        CONSTRAINT ck_recommendation_evidence_freshness CHECK (freshness IN
        ('fresh','stale','unknown','not_applicable'))
        )
    """)


def downgrade():
    if context.is_offline_mode():
        raise RuntimeError("0013 downgrade requires online validation of retained results")
    connection = op.get_bind()
    # Prevent inserts racing the emptiness check; never discard historical context.
    names = (
        "recommendation_results",
        "recommendation_cards",
        "recommendation_evidence",
        "recommendation_search_areas",
    )
    connection.execute(sa.text("LOCK TABLE " + ", ".join(names) + " IN ACCESS EXCLUSIVE MODE"))
    for name in names:
        if connection.scalar(sa.text(f"SELECT EXISTS (SELECT 1 FROM {name})")):
            raise RuntimeError("0013 downgrade refuses to discard recommendation context")
    op.drop_table("recommendation_search_areas")
    op.drop_table("recommendation_evidence")
    op.drop_table("recommendation_cards")
    op.drop_table("recommendation_results")
