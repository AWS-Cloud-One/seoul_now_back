import asyncio
import hashlib
import json
import runpy
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from alembic import context
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import PROJECT_ROOT, Settings
from app.db import session
from app.db.connection import connection_args

CA = PROJECT_ROOT / "infra/certificates/ap-northeast-2-bundle.pem"
URL = "postgresql+psycopg://user:synthetic-password@db.example/test"


def aws_settings(**overrides):
    values = dict(
        _env_file=None,
        runtime_env="aws",
        runtime_role="migration",
        settings_loaded=True,
        settings_secret_id="test-arn",
        database_url=URL,
        db_sslmode="verify-full",
        db_sslrootcert=CA,
    )
    return Settings(**(values | overrides))


@pytest.mark.parametrize("mode", [None, "disable", "allow", "prefer", "require", "verify-ca"])
def test_aws_rejects_every_mode_without_hostname_verification(mode):
    with pytest.raises(ValidationError, match="AWS requires DB_SSLMODE=verify-full"):
        aws_settings(db_sslmode=mode)


@pytest.mark.parametrize("path", [None, Path("relative.pem"), Path("/missing/ca.pem")])
def test_aws_requires_explicit_absolute_existing_ca(path):
    with pytest.raises(ValidationError):
        aws_settings(db_sslrootcert=path)


def test_ca_must_be_readable_and_parseable(monkeypatch, tmp_path):
    bad = tmp_path / "invalid.pem"
    bad.write_text("synthetic-secret")
    with pytest.raises(ValidationError) as exc:
        aws_settings(db_sslrootcert=bad)
    assert "synthetic-secret" not in str(exc.value)
    with pytest.raises(ValidationError):
        aws_settings(db_sslrootcert=tmp_path)
    monkeypatch.setattr("ssl.SSLContext.load_verify_locations", Mock(side_effect=PermissionError))
    with pytest.raises(ValidationError, match="Database CA file must be readable"):
        aws_settings()


@pytest.mark.parametrize(
    "query",
    [
        "sslmode=disable",
        "sslmode=verify-full&sslmode=verify-full",
        "sslrootcert=/wrong.pem",
        "sslrootcert=x&sslrootcert=y",
        "gssencmode=prefer",
        "gssencmode=disable&gssencmode=require",
        "host=/tmp",
        "hostaddr=127.0.0.1",
        "service=other",
        "requiressl=0",
        "options=-csearch_path=other",
    ],
)
def test_aws_rejects_tls_conflicts_and_connection_overrides(query):
    with pytest.raises(ValidationError) as exc:
        aws_settings(database_url=URL + "?" + query)
    assert "synthetic-password" not in str(exc.value)


@pytest.mark.parametrize("host", ["@socket", "db.example,", "/tmp"])
def test_aws_cannot_use_socket_or_multiple_hosts(host):
    from sqlalchemy.engine import URL as DatabaseURL

    url = DatabaseURL.create(
        "postgresql+psycopg",
        username="user",
        password="synthetic-password",
        host=host,
        database="test",
    )
    with pytest.raises(ValidationError):
        aws_settings(database_url=url.render_as_string(hide_password=False))


def test_matching_url_tls_options_are_allowed():
    settings = aws_settings(database_url=URL + f"?sslmode=verify-full&sslrootcert={CA}")
    assert connection_args(settings) == {
        "connect_timeout": 3,
        "sslmode": "verify-full",
        "sslrootcert": str(CA),
        "gssencmode": "disable",
    }


def test_local_defaults_and_existing_url_options_remain_supported():
    assert connection_args(Settings(_env_file=None)) == {"connect_timeout": 3}
    local = Settings(_env_file=None, database_url=URL + "?sslmode=disable")
    assert connection_args(local) == {"connect_timeout": 3, "sslmode": "disable"}
    with pytest.raises(ValidationError, match="Conflicting or repeated"):
        Settings(_env_file=None, database_url=URL + "?sslmode=disable", db_sslmode="require")


def test_aws_options_override_libpq_environment(monkeypatch):
    monkeypatch.setenv("PGSSLMODE", "disable")
    monkeypatch.setenv("PGSSLROOTCERT", "/wrong.pem")
    monkeypatch.setenv("PGGSSENCMODE", "require")
    args = connection_args(aws_settings())
    assert args["sslmode"] == "verify-full"
    assert args["sslrootcert"] == str(CA)
    assert args["gssencmode"] == "disable"


def test_api_and_jobs_keep_pool_policy(monkeypatch):
    create = Mock(spec=AsyncEngine)
    factory = Mock(return_value=create)
    monkeypatch.setattr(session, "create_async_engine", factory)
    settings = aws_settings()
    assert session.create_engine(settings) is create
    assert factory.call_args.kwargs == {
        "pool_pre_ping": True,
        "pool_size": 5,
        "max_overflow": 5,
        "pool_timeout": 3,
        "connect_args": connection_args(settings),
    }


def test_alembic_uses_shared_tls_null_pool_and_public_schema(monkeypatch):
    settings = aws_settings()
    monkeypatch.setattr("app.core.config.Settings", lambda: settings)
    # Load the real Alembic env without running DDL, then inspect its engine boundary.
    monkeypatch.setattr(context, "config", Config(), raising=False)
    monkeypatch.setattr(context, "is_offline_mode", lambda: False)
    real_run = asyncio.run
    monkeypatch.setattr(asyncio, "run", lambda coro: coro.close())
    namespace = runpy.run_path(str(PROJECT_ROOT / "migrations/env.py"))
    monkeypatch.setattr(asyncio, "run", real_run)
    engine = Mock(spec=AsyncEngine)
    engine.connect = Mock(side_effect=RuntimeError("stop-before-DDL"))
    engine.dispose = AsyncMock()
    factory = Mock(return_value=engine)
    namespace["run_migrations_online"].__globals__["create_async_engine"] = factory
    with pytest.raises(RuntimeError, match="stop-before-DDL"):
        asyncio.run(namespace["run_migrations_online"]())
    assert factory.call_args.kwargs == {
        "poolclass": pool.NullPool,
        "connect_args": {**connection_args(settings), "options": "-csearch_path=public"},
    }
    engine.dispose.assert_awaited_once()


def test_rds_bundle_matches_recorded_digest():
    metadata = json.loads((CA.parent / "sources.json").read_text())[CA.name]
    digest = hashlib.sha256(CA.read_bytes()).hexdigest()
    assert metadata["sha256"] == digest
    assert (CA.parent / "SHA256SUMS").read_text().strip() == f"{digest}  {CA.name}"
