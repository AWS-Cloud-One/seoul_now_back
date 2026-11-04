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


@pytest.fixture(autouse=True)
def isolated_payload_storage(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_PAYLOAD_STORAGE_DIR", str(tmp_path / "payloads"))


@pytest.fixture
def normalized_database(monkeypatch):
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
        command.upgrade(cfg, "head")
        yield test_url
    finally:
        with admin.connect() as c:
            c.exec_driver_sql(f'DROP DATABASE "{database}" WITH (FORCE)')
        admin.dispose()
