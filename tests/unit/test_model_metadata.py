"""Validate the active models' relational PostgreSQL schema."""

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from app.models import Base

# Disposable, expiring API state shared between processes. It holds no business data,
# so JSONB is allowed here only; every business table must stay relational.
TEMPORARY_STATE_TABLES = {
    "query_cursors",
    "read_replays",
    "trip_progress",
    "trip_requests",
    "origin_contexts",
}


def test_runtime_models_use_relational_postgresql_schema():
    # 0013 adds result/card/evidence and compact search-area snapshots.
    # 0015/0016 add disposable shared API state (cursors, replays, trip progress, origins).
    assert len(Base.metadata.tables) == 43
    assert "provider_snapshots" not in Base.metadata.tables
    for table in Base.metadata.sorted_tables:
        if table.name in TEMPORARY_STATE_TABLES:
            continue
        assert all(not isinstance(c.type, postgresql.JSON) for c in table.columns)
        assert str(CreateTable(table).compile(dialect=postgresql.dialect()))
        for index in table.indexes:
            assert str(CreateIndex(index).compile(dialect=postgresql.dialect()))


def test_business_models_have_no_raw_payload_foreign_keys():
    assert not {"raw_payloads", "legacy_archives", "payload_file_deletions"} & set(
        Base.metadata.tables
    )
    for table in Base.metadata.tables.values():
        assert all(fk.column.table.name != "raw_payloads" for fk in table.foreign_keys)
    assert "received_at" in Base.metadata.tables["population_latest"].c
    assert "received_at" in Base.metadata.tables["map_population_observations"].c


def test_snapshot_schema_is_resolvable_without_ambiguous_constraint_names():
    # PostgreSQL rejects duplicate constraint/index names even when DDL compiles.
    tables = [t for t in Base.metadata.tables.values() if t.name.startswith("recommendation_")]
    for table in tables:
        names = [c.name for c in table.constraints if c.name]
        assert len(names) == len(set(names)), table.name
        for foreign_key in table.foreign_keys:
            assert foreign_key.column.table in Base.metadata.tables.values()
    cards = Base.metadata.tables["recommendation_cards"]
    assert not cards.c.place_location.nullable
    assert {fk.column.table.name for fk in cards.foreign_keys} == {"recommendation_results"}
    results = Base.metadata.tables["recommendation_results"]
    assert "context_token" not in results.c
    assert not results.c.context_token_digest.nullable
    assert results.c.created_at.server_default is None
    assert results.c.context_expires_at.server_default is None
