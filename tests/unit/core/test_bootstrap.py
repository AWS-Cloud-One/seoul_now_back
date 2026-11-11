import json
import os
import subprocess
import sys
from unittest.mock import Mock, patch

import boto3
import pytest
from botocore.exceptions import NoCredentialsError, ReadTimeoutError
from botocore.stub import Stubber

from app import bootstrap
from app.core import secrets
from app.core.config import PROJECT_ROOT
from tests.unit.core.test_secrets import API_SECRET, ARN, DB_URL


@pytest.fixture
def aws_process_environment():
    with patch.dict(
        os.environ,
        {
            "APP_RUNTIME_ENV": "aws",
            "APP_RUNTIME_ROLE": "api",
            "APP_SETTINGS_SECRET_ID": ARN,
            "AWS_REGION": "ap-northeast-2",
            "APP_DB_SSLMODE": "verify-full",
            "APP_DB_SSLROOTCERT": str(
                PROJECT_ROOT / "infra/certificates/ap-northeast-2-bundle.pem"
            ),
        },
        clear=True,
    ):
        yield


@pytest.mark.parametrize(
    ("role", "provider", "command", "code"),
    [
        ("api", "openai", ["uvicorn"], "secret_missing_openai_key"),
        (
            "import",
            "bedrock",
            ["python", "-m", "jobs.import_topis_geometry"],
            "secret_missing_citydata_key",
        ),
        ("api", "unknown", ["uvicorn"], "invalid_application_settings"),
    ],
)
def test_conditional_required_settings_fail_before_exec(
    monkeypatch, capsys, aws_process_environment, role, provider, command, code
):
    os.environ.update(
        APP_RUNTIME_ROLE=role,
        APP_AI_PROVIDER=provider,
        APP_OPENAI_API_KEY="stale-should-not-be-used",
    )
    payload = API_SECRET if role == "api" else {"APP_DATABASE_URL": DB_URL}
    monkeypatch.setattr(secrets, "load_secret", Mock(return_value=payload))
    execute = Mock()
    monkeypatch.setattr(bootstrap.os, "execvpe", execute)
    assert bootstrap.main(command) == 1
    execute.assert_not_called()
    assert json.loads(capsys.readouterr().err) == {"status": "failed", "error_code": code}


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (NoCredentialsError(), "secret_connection_failed"),
        (ReadTimeoutError(endpoint_url="synthetic-secret"), "secret_connection_failed"),
        (ValueError("synthetic-secret"), "bootstrap_failed"),
    ],
)
def test_failure_output_is_safe(monkeypatch, capsys, aws_process_environment, failure, code):
    monkeypatch.setattr(secrets, "load_secret", Mock(side_effect=failure))
    execute = Mock()
    monkeypatch.setattr(bootstrap.os, "execvpe", execute)
    assert bootstrap.main(["uvicorn"]) == 1
    execute.assert_not_called()
    assert json.loads(capsys.readouterr().err) == {"status": "failed", "error_code": code}


def test_access_denied_is_sanitized_at_process_boundary(
    monkeypatch, capsys, aws_process_environment
):
    client = boto3.client(
        "secretsmanager",
        region_name="ap-northeast-2",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )
    monkeypatch.setattr(
        secrets.boto3, "Session", Mock(return_value=Mock(client=Mock(return_value=client)))
    )
    execute = Mock()
    monkeypatch.setattr(bootstrap.os, "execvpe", execute)
    with Stubber(client) as stub:
        stub.add_client_error("get_secret_value", "AccessDeniedException", "synthetic-secret")
        assert bootstrap.main(["uvicorn"]) == 1
    execute.assert_not_called()
    assert json.loads(capsys.readouterr().err)["error_code"] == "secret_access_failed"


def test_invalid_app_settings_are_sanitized(monkeypatch, capsys, aws_process_environment):
    monkeypatch.setattr(
        secrets,
        "load_secret",
        Mock(return_value={**API_SECRET, "APP_DATABASE_URL": "synthetic-secret"}),
    )
    assert bootstrap.main(["uvicorn"]) == 1
    assert json.loads(capsys.readouterr().err)["error_code"] == "invalid_application_settings"


def test_exec_error_and_missing_command_are_sanitized(monkeypatch, capsys):
    monkeypatch.setenv("APP_RUNTIME_ENV", "local")
    monkeypatch.setattr(
        bootstrap.os, "execvpe", Mock(side_effect=FileNotFoundError("synthetic-secret"))
    )
    assert bootstrap.main([]) == 1
    assert json.loads(capsys.readouterr().err)["error_code"] == "command_required"
    assert bootstrap.main(["missing"]) == 1
    assert json.loads(capsys.readouterr().err)["error_code"] == "command_start_failed"


@pytest.mark.parametrize("role", ["api", "collector", "migration", "import"])
def test_real_exec_and_child_inherit_settings_without_refetch(role, tmp_path):
    # Synthetic payload only. The actual process boundary uses exec; no test mode in app code.
    probe = tmp_path / "probe.py"
    probe.write_text("""
import asyncio, json, os, sys
import boto3
from app.core.config import Settings
from jobs.scheduler import Task, run_command
def forbidden(*args, **kwargs):
    raise AssertionError("child tried to fetch secrets")
boto3.Session = forbidden
for _ in range(2):
    settings = Settings()
    assert settings.database_url.get_secret_value().endswith("@db.example/test")
    assert not settings.seoul_api_key.get_secret_value()
if len(sys.argv) == 1:
    assert os.getpid() == int(os.environ["EXPECTED_PID"])
    task = Task("probe", 10, (), args=("child",))
    assert asyncio.run(run_command(task)) == 0
else:
    print(json.dumps({"role": settings.runtime_role, "inherited": True}))
""")
    launcher = """
import os, sys
from app import bootstrap
from app.core import secrets
calls = 0
def load(request):
    global calls
    calls += 1
    assert calls == 1
    return {
        "APP_DATABASE_URL": "postgresql+psycopg://test:synthetic-password@db.example/test",
        **({"APP_KAKAO_REST_API_KEY": "synthetic-kakao",
            "APP_SEOUL_CITYDATA_API_KEY": "synthetic-citydata"}
           if request.role == "api" else {})
    }
secrets.load_secret = load
os.environ["EXPECTED_PID"] = str(os.getpid())
sys.exit(bootstrap.main([sys.executable, sys.argv[1]]))
"""
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith(("APP_", "AWS_"))
    }
    environment.update(
        APP_RUNTIME_ENV="aws",
        APP_RUNTIME_ROLE=role,
        APP_SETTINGS_SECRET_ID=ARN,
        AWS_REGION="ap-northeast-2",
        APP_SEOUL_API_KEY="stale-secret",
        APP_DB_SSLMODE="verify-full",
        APP_DB_SSLROOTCERT=str(PROJECT_ROOT / "infra/certificates/ap-northeast-2-bundle.pem"),
        PYTHONPATH=os.pathsep.join([str(tmp_path), os.getcwd()]),
    )
    result = subprocess.run(
        [sys.executable, "-c", launcher, str(probe)],
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"role": role, "inherited": True}


def test_local_bootstrap_preserves_command_exit_status_and_literal_arguments():
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith(("APP_", "AWS_"))
    }
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "app.bootstrap",
            sys.executable,
            "-c",
            "import sys; print(sys.argv[1]); sys.exit(7)",
            "$(not-a-shell-command)",
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 7
    assert result.stdout.strip() == "$(not-a-shell-command)"
