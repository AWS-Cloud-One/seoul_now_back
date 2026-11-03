"""Validate the active models' relational PostgreSQL schema."""

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from app.models import Base


def test_runtime_models_use_relational_postgresql_schema():
    assert len(Base.metadata.tables) == 27
    assert "provider_snapshots" not in Base.metadata.tables
    for table in Base.metadata.sorted_tables:
        assert all(not isinstance(c.type, postgresql.JSON) for c in table.columns)
        assert str(CreateTable(table).compile(dialect=postgresql.dialect()))
        for index in table.indexes:
            assert str(CreateIndex(index).compile(dialect=postgresql.dialect()))
