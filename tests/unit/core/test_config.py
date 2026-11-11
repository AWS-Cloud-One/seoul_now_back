import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import PROJECT_ROOT, Settings
from app.main import create_app


def test_environment_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_CORS_ORIGINS", '["http://localhost:3000"]')
    monkeypatch.setenv("APP_DOCS_ENABLED", "false")
    settings = Settings(_env_file=None)
    assert settings.cors_origins == ["http://localhost:3000"]
    assert settings.docs_enabled is False


def test_ai_selection_and_bedrock_settings_from_environment(monkeypatch):
    assert Settings(_env_file=None).ai_provider == "bedrock"
    monkeypatch.setenv("APP_AI_PROVIDER", "openai")
    monkeypatch.setenv("APP_BEDROCK_MODEL_ID", "  chosen-model  ")
    monkeypatch.setenv("APP_BEDROCK_REGION", "ap-northeast-2")
    monkeypatch.setenv("APP_BEDROCK_MAX_TOKENS", "1024")
    settings = Settings(_env_file=None)
    assert settings.ai_provider == "openai"
    assert settings.bedrock_model_id == "chosen-model"
    assert settings.bedrock_max_tokens == 1024


@pytest.mark.parametrize(
    "values",
    [
        {"ai_provider": "unknown"},
        {"bedrock_model_id": " "},
        {"bedrock_region": ""},
        {"bedrock_max_tokens": 0},
        {"bedrock_timeout_seconds": 0},
    ],
)
def test_invalid_ai_configuration_is_rejected(values):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **values)


def test_context_ttl_from_environment(monkeypatch):
    monkeypatch.setenv("APP_RESULT_CONTEXT_TTL_SECONDS", "3600")
    assert Settings(_env_file=None).result_context_ttl_seconds == 3600
    monkeypatch.setenv("APP_RESULT_CONTEXT_TTL_SECONDS", "0")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


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


def test_aws_never_reads_dotenv_even_when_explicitly_supplied(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("APP_SEOUL_API_KEY=dotenv-secret\nAPP_DOCS_ENABLED=false\n")
    monkeypatch.setenv("APP_RUNTIME_ENV", "aws")
    settings = Settings(
        _env_file=env_file,
        runtime_role="migration",
        settings_loaded=True,
        settings_secret_id="test-arn",
        database_url="postgresql+psycopg://u:p@db/test",
        db_sslmode="verify-full",
        db_sslrootcert=PROJECT_ROOT / "infra/certificates/ap-northeast-2-bundle.pem",
    )
    assert not settings.seoul_api_key.get_secret_value()
    assert settings.docs_enabled is True


@pytest.mark.parametrize(
    "values",
    [
        {},
        {"database_url": "postgresql+psycopg://u:p@db/test"},
        {"runtime_role": "migration", "settings_loaded": True, "settings_secret_id": "test-arn"},
    ],
)
def test_aws_cannot_start_with_local_defaults_or_bypass_bootstrap(monkeypatch, values):
    monkeypatch.delenv("APP_DATABASE_URL", raising=False)
    with pytest.raises(ValidationError, match="AWS settings must be supplied by app.bootstrap"):
        Settings(_env_file=None, runtime_env="aws", **values)


def test_local_dotenv_remains_supported(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("APP_SEOUL_API_KEY=synthetic-local-key\n")
    settings = Settings(_env_file=env_file, runtime_env="local")
    assert settings.seoul_api_key.get_secret_value() == "synthetic-local-key"
