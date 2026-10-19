import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import Settings
from app.main import create_app


def test_environment_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_CORS_ORIGINS", '["http://localhost:3000"]')
    monkeypatch.setenv("APP_DOCS_ENABLED", "false")
    settings = Settings(_env_file=None)
    assert settings.cors_origins == ["http://localhost:3000"]
    assert settings.docs_enabled is False


@pytest.mark.parametrize("url", ["sqlite:///test.db", "postgresql://u:p@localhost/db"])
def test_database_driver_must_match_project(url: str) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, database_url=url)


def test_database_credentials_are_redacted() -> None:
    settings = Settings(
        _env_file=None,
        database_url="postgresql+psycopg://user:private-password@localhost/db",
    )
    assert "private-password" not in repr(settings)


def test_documentation_can_be_disabled() -> None:
    app = create_app(Settings(_env_file=None, docs_enabled=False))
    with TestClient(app) as client:
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404
