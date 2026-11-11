"""Run explicitly: python -m tests.smoke.runtime_settings --image seoul-now-backend:w2a.

Uses disposable, network-disabled containers and synthetic SDK responses only.
Does not mount credentials, connect to a DB, or restart existing containers.
"""

import argparse
import json
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

HOOK = """
import json, os, sys
import boto3
from botocore.stub import Stubber

def forbidden(*args, **kwargs):
    raise AssertionError("unexpected AWS access")

if os.environ.get("APP_SETTINGS_LOADED") == "1" or os.environ.get("APP_RUNTIME_ENV") != "aws":
    boto3.Session = forbidden
else:
    role = os.environ["APP_RUNTIME_ROLE"]
    values = {"APP_DATABASE_URL": "postgresql+psycopg://test:synthetic-password@db.example/test"}
    if role == "api":
        values.update(APP_KAKAO_REST_API_KEY="synthetic-kakao",
                      APP_SEOUL_CITYDATA_API_KEY="synthetic-citydata")
    if os.environ.get("W2A_OPENAI_KEY") == "1":
        values["APP_OPENAI_API_KEY"] = "synthetic-openai"
    if os.environ.get("W2A_CITYDATA_KEY") == "1":
        values["APP_SEOUL_CITYDATA_API_KEY"] = "synthetic-citydata"
    client = boto3.client("secretsmanager", region_name="ap-northeast-2",
                          aws_access_key_id="testing", aws_secret_access_key="testing")
    stub = Stubber(client)
    stub.add_response("get_secret_value", {"SecretString": json.dumps(values)},
                      {"SecretId": os.environ["APP_SETTINGS_SECRET_ID"],
                       "VersionStage": "AWSCURRENT"})
    stub.activate()
    original_get = client.get_secret_value
    def get(**kwargs):
        result = original_get(**kwargs)
        print("w2a-secret-read", file=sys.stderr)
        return result
    client.get_secret_value = get
    class Session:
        def client(self, service, **kwargs):
            assert service == "secretsmanager"
            return client
    boto3.Session = Session
"""


def docker(*args, check=True):
    result = subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=30, check=False
    )
    if check and result.returncode:
        raise AssertionError(result.stderr)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    image = parser.parse_args().image
    config = json.loads(docker("image", "inspect", image).stdout)[0]["Config"]
    assert config["Entrypoint"] == ["python", "-m", "app.bootstrap"]
    assert config["User"] == "app"
    assert config["Cmd"][0] == "uvicorn"
    with tempfile.TemporaryDirectory(prefix="seoul-now-w2a-") as temp:
        hook_dir = Path(temp)
        hook_dir.chmod(0o755)
        (hook_dir / "sitecustomize.py").write_text(HOOK)
        common = [
            "--network",
            "none",
            "--mount",
            f"type=bind,src={hook_dir},dst=/testhooks,readonly",
            "-e",
            "PYTHONPATH=/testhooks:/app",
        ]

        def aws(role, extra=()):
            return [
                *common,
                "-e",
                "APP_RUNTIME_ENV=aws",
                "-e",
                f"APP_RUNTIME_ROLE={role}",
                "-e",
                "AWS_REGION=ap-northeast-2",
                "-e",
                "APP_DB_SSLMODE=verify-full",
                "-e",
                "APP_DB_SSLROOTCERT=/app/certs/rds/ap-northeast-2-bundle.pem",
                "-e",
                f"APP_SETTINGS_SECRET_ID=arn:aws:secretsmanager:ap-northeast-2:123456789012:secret:seoul-now/lab/{role}-a1B2c3",
                *extra,
            ]

        local = docker(
            "run",
            "--rm",
            *common,
            image,
            "python",
            "-c",
            """
import os
from pathlib import Path
from app.core.config import Settings
assert os.getuid() == 10001
assert Settings().runtime_env == "local"
assert not any(Path("/app").glob(".env*"))
print("local-ok")
""",
        )
        assert local.stdout.strip() == "local-ok" and not local.stderr
        print("PASS local/no AWS/no env files/non-root", flush=True)

        for provider in ("bedrock", "openai"):
            name = f"seoul-now-w2a-{uuid.uuid4().hex[:10]}"
            extra = ["-e", f"APP_AI_PROVIDER={provider}"]
            if provider == "openai":
                extra += ["-e", "W2A_OPENAI_KEY=1"]
            try:
                docker("run", "-d", "--name", name, *aws("api", extra), image)
                health = None
                for _ in range(30):
                    health = docker(
                        "exec",
                        name,
                        "python",
                        "-c",
                        "import urllib.request; assert urllib.request.urlopen("
                        "'http://127.0.0.1:8000/api/v1/health/live', timeout=1).status == 200",
                        check=False,
                    )
                    if health.returncode == 0:
                        break
                    time.sleep(0.2)
                assert health is not None and health.returncode == 0, docker("logs", name).stderr
                docker(
                    "exec",
                    name,
                    "python",
                    "-c",
                    "from pathlib import Path; "
                    "assert b'uvicorn' in Path('/proc/1/cmdline').read_bytes()",
                )
                docker("stop", "--time", "5", name)
                state = json.loads(docker("inspect", name).stdout)[0]["State"]
                assert state["ExitCode"] == 0
                assert docker("logs", name).stderr.count("w2a-secret-read") == 1
                print(f"PASS API/{provider}/live/PID1/TERM/one secret fetch", flush=True)
            finally:
                docker("rm", "-f", name, check=False)

        commands = [
            (
                "collector",
                (),
                ["python", "-u", "-m", "jobs.scheduler", "--once"],
                3,
                "missing_api_key",
            ),
            ("migration", (), ["alembic", "upgrade", "0014:head", "--sql"], 0, "COMMIT"),
            ("import", (), ["python", "-m", "jobs.import_place_boundaries", "--help"], 0, "usage:"),
            ("import", (), ["python", "-m", "jobs.import_sensor_locations", "--help"], 0, "usage:"),
            (
                "import",
                ("-e", "W2A_CITYDATA_KEY=1"),
                ["python", "-m", "jobs.import_topis_geometry", "--help"],
                0,
                "usage:",
            ),
            (
                "import",
                (),
                ["python", "-m", "jobs.import_topis_geometry", "--help"],
                1,
                "secret_missing_citydata_key",
            ),
            ("api", ("-e", "APP_AI_PROVIDER=openai"), ["uvicorn"], 1, "secret_missing_openai_key"),
        ]
        for role, extra, command, status, expected in commands:
            result = docker("run", "--rm", *aws(role, extra), image, *command, check=False)
            assert result.returncode == status, result.stderr
            assert expected in result.stdout + result.stderr, result.stdout + result.stderr
            assert "synthetic-password" not in result.stdout + result.stderr
            assert result.stderr.count("w2a-secret-read") == 1
            print(f"PASS {role}/{expected}/exit={status}/one secret fetch", flush=True)


if __name__ == "__main__":
    main()
