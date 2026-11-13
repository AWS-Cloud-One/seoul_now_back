"""The read-only revision report used by the deploy/rollback compatibility gate."""

import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from app.core.config import Settings
from infra.cicd.db_compat import Refused, decide, load_graph
from infra.scripts.w4_database import revision

pytestmark = pytest.mark.integration
MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations" / "versions"


def report_for(monkeypatch, capsys, role):
    monkeypatch.setenv("APP_RUNTIME_ROLE", role)
    revision(Settings())
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


@pytest.mark.parametrize("normalized_database", ["0015"], indirect=True)
def test_revision_reports_database_and_image_without_changing_anything(
    normalized_database, monkeypatch, capsys
):
    report = report_for(monkeypatch, capsys, "migration")
    assert report["check"] == "w4_revision"
    assert report["db_revision"] == "0015"
    graph = load_graph(MIGRATIONS)
    head = max(graph)
    assert report["image_head"] == head
    assert report["image_revisions"] == sorted(graph)

    # The current image includes incompatible V2 migrations. A normal release
    # must refuse this old database, and rollback cannot upgrade it either.
    with pytest.raises(Refused, match="not backward_compatible"):
        decide("release", graph, report)
    with pytest.raises(Refused, match="normal deploy"):
        decide("rollback", graph, report)

    engine = create_engine(normalized_database)
    try:
        with engine.connect() as connection:
            assert connection.execute(text("SELECT version_num FROM alembic_version")).all() == [
                ("0015",)
            ]
    finally:
        engine.dispose()


@pytest.mark.parametrize("normalized_database", ["0015"], indirect=True)
def test_revision_requires_the_migration_role(normalized_database, monkeypatch):
    monkeypatch.setenv("APP_RUNTIME_ROLE", "api")
    with pytest.raises(RuntimeError, match="migration_role_required"):
        revision(Settings())
