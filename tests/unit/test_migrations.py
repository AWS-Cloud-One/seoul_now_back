"""Revision graph regressions do not need a running database."""

import ast
import io
import warnings
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory


def test_migration_revisions_are_unique_with_one_head():
    with warnings.catch_warnings():
        warnings.simplefilter("error", UserWarning)
        scripts = ScriptDirectory.from_config(Config("alembic.ini"))
        revisions = list(scripts.walk_revisions())
        assert scripts.get_heads() == ["0023"]
        assert {item.revision for item in revisions} == {f"{n:04}" for n in range(1, 24)}
        assert len({item.revision for item in revisions}) == len(revisions)
        assert scripts.get_revision("0009").down_revision == "0008"
        assert scripts.get_revision("0010").down_revision == "0009"
        assert scripts.get_revision("0011").down_revision == "0010"
        assert scripts.get_revision("0012").down_revision == "0011"
        assert scripts.get_revision("0013").down_revision == "0012"
        assert scripts.get_revision("0014").down_revision == "0013"
        assert scripts.get_revision("0015").down_revision == "0014"
        assert scripts.get_revision("0016").down_revision == "0015"
        assert scripts.get_revision("0017").down_revision == "0016"
        assert scripts.get_revision("0018").down_revision == "0017"
        assert scripts.get_revision("0019").down_revision == "0018"
        assert scripts.get_revision("0020").down_revision == "0019"
        assert scripts.get_revision("0021").down_revision == "0020"
        assert scripts.get_revision("0022").down_revision == "0021"
        assert scripts.get_revision("0023").down_revision == "0022"


def test_0013_frozen_upgrade_only_adds_result_context_tables(monkeypatch):
    # Render exactly this revision without connecting to a database or depending
    # on a future version of app.models to build its DDL.
    source = Path("migrations/versions/0013_recommendation_context.py").read_text()
    imports = [node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.ImportFrom)]
    assert all(not (node.module or "").startswith("app") for node in imports)
    output = io.StringIO()
    config = Config("alembic.ini", output_buffer=output)
    monkeypatch.setenv("APP_DATABASE_URL", "postgresql+psycopg://test:test@127.0.0.1:1/test")
    command.upgrade(config, "0012:0013", sql=True)
    sql = " ".join(output.getvalue().split())
    assert sql.count("CREATE TABLE ") == 4
    assert all(
        f"CREATE TABLE {name}" in sql
        for name in (
            "recommendation_results",
            "recommendation_cards",
            "recommendation_evidence",
            "recommendation_search_areas",
        )
    )
    assert "ALTER TABLE" not in sql and "DROP TABLE" not in sql
    assert "INSERT INTO" not in sql and "DELETE FROM" not in sql
    assert "context_expires_at > created_at" in sql
    assert "REFERENCES chat_condition_versions (trip_id, version)" in sql
    assert "REFERENCES recommendation_results (trip_id, result_id)" in sql
    assert "REFERENCES recommendation_cards (result_id, place_id, result_kind)" in sql
    assert "BEGIN;" in sql and "COMMIT;" in sql
