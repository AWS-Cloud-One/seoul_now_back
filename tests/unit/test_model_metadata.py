"""Validate the active models' relational PostgreSQL schema."""

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from app.models import Base


def test_runtime_models_use_relational_postgresql_schema():
    assert len(Base.metadata.tables) == 34
    assert "provider_snapshots" not in Base.metadata.tables
    for table in Base.metadata.sorted_tables:
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
