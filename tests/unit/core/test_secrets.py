import json
from contextlib import closing
from unittest.mock import Mock

import boto3
import pytest
from botocore.config import Config
from botocore.exceptions import ReadTimeoutError
from botocore.stub import Stubber
from pydantic import SecretStr

from app.core import secrets
from app.core.config import Settings
from app.core.secrets import SecretRequest, StartupError, load_secret, parse_secret

ARN = "arn:aws:secretsmanager:ap-northeast-2:123456789012:secret:seoul-now/lab/test-a1B2c3"
DB_URL = "postgresql+psycopg://test:synthetic-password@db.example/test"
API_SECRET = {
    "APP_DATABASE_URL": DB_URL,
    "APP_KAKAO_REST_API_KEY": "synthetic-kakao",
    "APP_SEOUL_CITYDATA_API_KEY": "synthetic-citydata",
}


@pytest.fixture
def aws_environment():
    return {
        "APP_RUNTIME_ENV": "aws",
        "APP_RUNTIME_ROLE": "migration",
        "APP_SETTINGS_SECRET_ID": ARN,
        "AWS_REGION": "ap-northeast-2",
    }


@pytest.fixture
def secret_client(monkeypatch):
    with closing(
        boto3.client(
            "secretsmanager",
            region_name="ap-northeast-2",
            aws_access_key_id="testing",
            aws_secret_access_key="testing",
            config=Config(retries={"total_max_attempts": 1}),
        )
    ) as client:
        factory = Mock(return_value=client)
        monkeypatch.setattr(secrets.boto3, "Session", Mock(return_value=Mock(client=factory)))
        client.close = Mock(wraps=client.close)
        yield client, factory


def test_sdk_reads_current_version_once_and_closes_client(secret_client):
    client, factory = secret_client
    request = SecretRequest("api", ARN, "ap-northeast-2")
    with Stubber(client) as stub:
        stub.add_response(
            "get_secret_value",
            {"SecretString": json.dumps(API_SECRET)},
            {"SecretId": ARN, "VersionStage": "AWSCURRENT"},
        )
        assert load_secret(request) == API_SECRET
        stub.assert_no_pending_responses()
    client.close.assert_called_once()
    factory.assert_called_once()
    assert factory.call_args.kwargs["region_name"] == "ap-northeast-2"
    config = factory.call_args.kwargs["config"]
    assert config.connect_timeout == 3
    assert config.read_timeout == 5
    assert config.retries == {"total_max_attempts": 2, "mode": "adaptive"}
    assert config.ignore_configured_endpoint_urls is True


@pytest.mark.parametrize(
    ("service_code", "safe_code"),
    [
        ("ResourceNotFoundException", "secret_not_found"),
        ("DecryptionFailure", "secret_decryption_failed"),
        ("InvalidRequestException", "secret_unavailable"),
    ],
)
def test_typed_sdk_errors_are_sanitized(secret_client, service_code, safe_code):
    client, _ = secret_client
    with Stubber(client) as stub:
        stub.add_client_error("get_secret_value", service_code, "synthetic-password")
        with pytest.raises(StartupError, match=f"^{safe_code}$"):
            load_secret(SecretRequest("migration", ARN, "ap-northeast-2"))
    client.close.assert_called_once()


def test_binary_secrets_are_not_accepted(secret_client):
    client, _ = secret_client
    with Stubber(client) as stub:
        stub.add_response("get_secret_value", {"SecretBinary": b"synthetic-password"})
        with pytest.raises(StartupError, match="^secret_string_required$"):
            load_secret(SecretRequest("migration", ARN, "ap-northeast-2"))


def test_timeout_still_closes_client(secret_client):
    client, _ = secret_client
    client.get_secret_value = Mock(side_effect=ReadTimeoutError(endpoint_url="synthetic-password"))
    with pytest.raises(ReadTimeoutError):
        load_secret(SecretRequest("migration", ARN, "ap-northeast-2"))
    client.close.assert_called_once()


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ("{", "secret_invalid_json"),
        ("[]", "secret_invalid_object"),
        ("null", "secret_invalid_object"),
        ('{"APP_DATABASE_URL":"one","APP_DATABASE_URL":"two"}', "secret_duplicate_key"),
        ('{"APP_DATABASE_URL":42}', "secret_invalid_value"),
        ('{"APP_DATABASE_URL":null}', "secret_invalid_value"),
        ('{"APP_DATABASE_URL":{}}', "secret_invalid_value"),
        ('{"APP_DATABASE_URL":true}', "secret_invalid_value"),
        ('{"APP_DATABASE_URL":"bad\\u0000value"}', "secret_invalid_value"),
        ('{"APP_DATABASE_URL":"bad\\ud800value"}', "secret_invalid_value"),
        ('{"APP_DATABASE_URL":"  "}', "secret_missing_required"),
        ('{"APP_DATABASE_URL":"ok","APP_AI_PROVIDER":"openai"}', "secret_unexpected_key"),
        ('{"APP_DATABASE_URL":"ok","AWS_ACCESS_KEY_ID":"key"}', "secret_unexpected_key"),
        ('{"APP_DATABASE_URL":"ok","PATH":"evil"}', "secret_unexpected_key"),
        ('{"APP_DATABASE_URL":"ok","APP_OPENAI_API_KEY":"key"}', "secret_unexpected_key"),
        ("{}", "secret_missing_required"),
    ],
)
def test_malformed_or_cross_role_payload_is_rejected(payload, code):
    with pytest.raises(StartupError, match=f"^{code}$"):
        parse_secret(payload, "migration")


@pytest.mark.parametrize("missing", list(API_SECRET))
def test_api_requires_all_mvp_keys(missing):
    values = {key: value for key, value in API_SECRET.items() if key != missing}
    with pytest.raises(StartupError, match="^secret_missing_required$"):
        parse_secret(json.dumps(values), "api")


@pytest.mark.parametrize("role", ["collector", "migration", "import"])
def test_non_api_roles_allow_db_only(role):
    values = {"APP_DATABASE_URL": DB_URL}
    assert parse_secret(json.dumps(values), role) == values


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("APP_RUNTIME_ROLE", ""),
        ("APP_RUNTIME_ROLE", "unknown"),
        ("APP_SETTINGS_SECRET_ID", "seoul-now/lab/api"),
        ("APP_SETTINGS_SECRET_ID", ARN[:-7]),
        ("AWS_REGION", ""),
        ("AWS_REGION", "us-east-1"),
    ],
)
def test_bad_bootstrap_config_never_queries_aws(monkeypatch, aws_environment, key, value):
    loader = Mock()
    monkeypatch.setattr(secrets, "load_secret", loader)
    aws_environment[key] = value
    with pytest.raises(StartupError, match="^invalid_bootstrap_configuration$"):
        secrets.prepare_environment(aws_environment)
    loader.assert_not_called()


def test_secret_replaces_sensitive_environment_without_mutating_input(monkeypatch, aws_environment):
    aws_environment.update(
        APP_DATABASE_URL="stale-password",
        APP_OPENAI_API_KEY="stale-key",
        app_seoul_api_key="stale-lowercase-key",
        APP_ITS_API_KEY="stale-its-key",
        APP_DOCS_ENABLED="false",
        AWS_CONTAINER_CREDENTIALS_FULL_URI="http://169.254.170.23/v1/credentials",
    )
    loader = Mock(return_value={"APP_DATABASE_URL": DB_URL})
    monkeypatch.setattr(secrets, "load_secret", loader)
    result = secrets.prepare_environment(aws_environment)
    assert result["APP_DATABASE_URL"] == DB_URL
    assert not any("stale" in value for value in result.values())
    assert result["APP_DOCS_ENABLED"] == "false"
    assert result["AWS_CONTAINER_CREDENTIALS_FULL_URI"].endswith("/credentials")
    assert result["APP_SETTINGS_LOADED"] == "1"
    assert aws_environment["APP_DATABASE_URL"] == "stale-password"
    loader.assert_called_once()


def test_local_never_creates_an_aws_session(monkeypatch):
    session = Mock(side_effect=AssertionError("local attempted AWS access"))
    monkeypatch.setattr(secrets.boto3, "Session", session)
    for environment in ({}, {"APP_RUNTIME_ENV": "local", "APP_DATABASE_URL": DB_URL}):
        assert secrets.prepare_environment(environment) == environment
    session.assert_not_called()


def test_unknown_runtime_mode_is_not_treated_as_local():
    with pytest.raises(StartupError, match="^invalid_runtime_environment$"):
        secrets.prepare_environment({"APP_RUNTIME_ENV": "awss"})


def test_environment_cleanup_covers_every_sensitive_settings_field():
    sensitive_fields = {
        "APP_" + name.upper()
        for name, field in Settings.model_fields.items()
        if field.annotation is SecretStr
    }
    assert secrets.SENSITIVE_KEYS == sensitive_fields
