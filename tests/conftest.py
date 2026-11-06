"""Disposable databases for storage transition tests; never reset the supplied DB."""

import os
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def normalized_database(monkeypatch, request, tmp_path):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL required (isolated server with CREATEDB)")
    database = "normalized_" + uuid4().hex
    admin = sa.create_engine(url, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.exec_driver_sql(f'CREATE DATABASE "{database}" TEMPLATE template0')
    test_url = make_url(url).set(database=database).render_as_string(hide_password=False)
    monkeypatch.setenv("APP_DATABASE_URL", test_url)
    try:
        cfg = Config(str(ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(ROOT / "migrations"))
        if getattr(request, "param", "head") != "head":
            cfg.attributes["payload_storage_dir"] = tmp_path / "migration-archives"
        command.upgrade(cfg, getattr(request, "param", "head"))
        yield test_url
    finally:
        with admin.connect() as c:
            c.exec_driver_sql(f'DROP DATABASE "{database}" WITH (FORCE)')
        admin.dispose()


@pytest.fixture
def no_response_archive(monkeypatch):
    # Current schema has no archive tables or file-writing repository.
    from app.models import Base
    from app.repositories.collection import CollectionRepository

    assert "raw_payloads" not in Base.metadata.tables
    assert not hasattr(CollectionRepository, "archive")


@pytest.fixture
def no_runtime_files(no_response_archive, monkeypatch):
    """Seed references in DB, then reject any runtime dependency on repo data files."""
    import builtins
    import io

    def guarded(original):
        def open_file(path, *args, **kwargs):
            if isinstance(path, (str, os.PathLike)) and Path(path).resolve().is_relative_to(
                ROOT / "data"
            ):
                pytest.fail("Runtime tried to read/write data files: " + str(path))
            return original(path, *args, **kwargs)

        return open_file

    monkeypatch.setattr(builtins, "open", guarded(builtins.open))
    monkeypatch.setattr(io, "open", guarded(io.open))
