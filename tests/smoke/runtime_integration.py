"""Explicit W2 acceptance: python -m tests.smoke.runtime_integration --image IMAGE.

One immutable production image, real role commands, stubbed Secrets Manager/HTTP,
verified TLS PostGIS and the repository's public place-reference input files.
No host ports, real credentials, external requests or existing DBs are used.
"""

import argparse
import json
import shutil
import tempfile
import time
from pathlib import Path
from uuid import uuid4

from tests.fixtures.migrations import current_head
from tests.smoke.database_tls import POSTGIS, RDS_CA, certificates
from tests.smoke.runtime_settings import docker

ROOT = Path(__file__).resolve().parents[2]
PRIVATE_VALUES = (
    "synthetic-password",
    "synthetic-seoul",
    "synthetic-kakao",
    "synthetic-citydata",
    "stale-password",
)


def safe_output(result):
    output = result.stdout + result.stderr
    assert not any(value in output for value in PRIVATE_VALUES), "secret appeared in logs"
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    info = json.loads(docker("image", "inspect", parser.parse_args().image).stdout)[0]
    image, config = info["Id"], info["Config"]
    assert config["User"] == "app"
    assert config["Entrypoint"] == ["python", "-m", "app.bootstrap"]
    assert config["Cmd"] == [
        "uvicorn",
        "app.main:create_app",
        "--factory",
        "--host",
        "0.0.0.0",
        "--port",
        "8000",
    ]
    print(f"IMAGE {image} {info['Os']}/{info['Architecture']}", flush=True)
    prefix = "seoul-now-w2d-" + uuid4().hex[:8]
    created = []
    with tempfile.TemporaryDirectory(prefix=prefix) as temp:
        directory = Path(temp)
        directory.chmod(0o755)
        certificates(directory)
        shutil.copyfile(
            Path(__file__).parent / "fixtures/runtime_integration_hook.py",
            directory / "sitecustomize.py",
        )
        for filename in ("seoul_areas.xlsx", "citydata_places_official.zip"):
            shutil.copyfile(ROOT / "data" / filename, directory / filename)
        docker("network", "create", "--internal", prefix)
        try:
            database = prefix + "-db"
            docker(
                "run",
                "-d",
                "--name",
                database,
                "--platform",
                "linux/amd64",
                "--network",
                prefix,
                "--network-alias",
                "db-tls",
                "--mount",
                f"type=bind,src={directory},dst=/checks,readonly",
                "--tmpfs",
                "/var/lib/postgresql/data:rw",
                "-e",
                "POSTGRES_USER=test",
                "-e",
                "POSTGRES_PASSWORD=synthetic-password",
                "-e",
                "POSTGRES_DB=test",
                POSTGIS,
                "bash",
                "-c",
                "install -o postgres -g postgres -m 600 /checks/server.key /tmp/server.key"
                " && exec docker-entrypoint.sh postgres -c ssl=on"
                " -c ssl_cert_file=/checks/server.pem -c ssl_key_file=/tmp/server.key",
            )
            created.append(database)
            for _ in range(50):
                if (
                    docker("exec", database, "pg_isready", "-U", "test", check=False).returncode
                    == 0
                ):
                    break
                time.sleep(0.2)
            else:
                raise AssertionError("test DB did not start")

            def sql(statement):
                return docker(
                    "exec",
                    database,
                    "psql",
                    "-U",
                    "test",
                    "-d",
                    "test",
                    "-At",
                    "-v",
                    "ON_ERROR_STOP=1",
                    "-c",
                    statement,
                ).stdout.strip()

            def options(role, extra=None):
                env = {
                    "PYTHONPATH": "/checks:/app",
                    "APP_RUNTIME_ENV": "aws",
                    "APP_RUNTIME_ROLE": role,
                    "AWS_REGION": "ap-northeast-2",
                    "APP_SETTINGS_SECRET_ID": (
                        "arn:aws:secretsmanager:ap-northeast-2:123456789012:"
                        f"secret:seoul-now/lab/{role}-a1B2c3"
                    ),
                    "APP_DB_SSLMODE": "verify-full",
                    "APP_DB_SSLROOTCERT": "/checks/ca.pem",
                    # Bootstrap must replace this stale connection and libpq settings.
                    "APP_DATABASE_URL": "postgresql+psycopg://old:stale-password@invalid/test",
                    "PGSSLMODE": "disable",
                    "PGGSSENCMODE": "require",
                }
                env.update(extra or {})
                args = [
                    "--network",
                    prefix,
                    "--mount",
                    f"type=bind,src={directory},dst=/checks,readonly",
                ]
                for key, value in env.items():
                    args += ["-e", f"{key}={value}"]
                return args

            def run(role, command, expected=0, extra=None):
                result = docker("run", "--rm", *options(role, extra), image, *command, check=False)
                output = safe_output(result)
                assert result.returncode == expected, output
                assert result.stderr.count("w2d-secret-read") == 1, output
                return result

            def start(label, role, command=(), extra=None):
                name = prefix + "-" + label
                docker("run", "-d", "--name", name, *options(role, extra), image, *command)
                created.append(name)
                return name

            def logs(name):
                return safe_output(docker("logs", name))

            def wait_log(name, marker):
                for _ in range(80):
                    if marker in logs(name):
                        return
                    state = json.loads(docker("inspect", name).stdout)[0]["State"]
                    assert state["Running"], logs(name)
                    time.sleep(0.15)
                raise AssertionError(f"missing marker {marker}: {logs(name)}")

            def stop(name, expected):
                docker("stop", "--time", "20", name)
                state = json.loads(docker("inspect", name).stdout)[0]["State"]
                assert state["ExitCode"] == expected, logs(name)
                assert logs(name).count("w2d-secret-read") == 1

            # Image contract is exercised through the real AWS bootstrap under UID 10001.
            run(
                "migration",
                [
                    "python",
                    "-c",
                    """
import hashlib, importlib.util, os
from pathlib import Path
assert os.getuid() == 10001
assert not any(Path('/app').glob('.env*'))
assert not Path('/app/tests').exists() and not Path('/app/data').exists()
assert importlib.util.find_spec('pytest') is None
root = Path('/app/certs/rds')
for line in (root / 'SHA256SUMS').read_text().splitlines():
    digest, name = line.split()
    assert hashlib.sha256((root / name).read_bytes()).hexdigest() == digest
""",
                ],
            )
            run("migration", ["python", "-m", "pip", "check"])
            print("PASS non-root/CA hashes/no env or test tools/dependencies", flush=True)

            bypass = docker(
                "run",
                "--rm",
                "--entrypoint",
                "python",
                *options("api"),
                image,
                "-c",
                "from app.core.config import Settings; Settings(); print('COMMAND-STARTED')",
                check=False,
            )
            output = safe_output(bypass)
            assert bypass.returncode == 1
            assert "AWS settings must be supplied by app.bootstrap" in output
            assert "w2d-secret-read" not in output and "COMMAND-STARTED" not in bypass.stdout
            print("PASS AWS Settings rejects bypassed ENTRYPOINT", flush=True)

            failure = run(
                "api", ["python", "-c", "print('COMMAND-STARTED')"], 1, {"TEST_SECRET_FAILURE": "1"}
            )
            assert (
                "secret_access_failed" in failure.stderr and "COMMAND-STARTED" not in failure.stdout
            )
            print("PASS Secret denied before command/no secret disclosure", flush=True)

            run("migration", ["alembic", "upgrade", "head"])
            run("migration", ["alembic", "check"])
            assert sql("SELECT version_num FROM alembic_version") == current_head()
            print("PASS AWS bootstrap/TLS/real migration/schema check", flush=True)

            command = [
                "python",
                "-m",
                "jobs.import_place_boundaries",
                "--areas-file",
                "/checks/seoul_areas.xlsx",
                "--boundaries-file",
                "/checks/citydata_places_official.zip",
            ]
            versions = []
            for _ in range(2):
                result = run("import", command)
                assert json.loads(result.stdout)["records"] == 121
                assert sql("SELECT count(*) FROM population_areas") == "121"
                versions.append(
                    sql("""
                    SELECT md5(string_agg(
                        area_code || ':' || source_version, ',' ORDER BY area_code))
                    FROM population_areas
                """)
                )
            assert versions[0] == versions[1]
            print(
                "PASS real import/TLS/121 place boundaries/repeat preserves row count", flush=True
            )

            api = start("api", "api")
            wait_log(api, "Application startup complete")

            def health(name, ready):
                docker(
                    "exec",
                    "-e",
                    "PYTHONPATH=",
                    name,
                    "python",
                    "-c",
                    f"""
import urllib.request, urllib.error
for path, expected in [('live', 200), ('ready', {ready})]:
    try:
        response = urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health/' + path, timeout=8)
    except urllib.error.HTTPError as error:
        response = error
    assert response.status == expected
    assert 'synthetic-password' not in response.read().decode()
""",
                )

            health(api, 200)
            assert (
                sql("""
                SELECT bool_and(s.ssl) FROM pg_stat_activity a JOIN pg_stat_ssl s USING(pid)
                WHERE a.application_name='w2d-api'
            """)
                == "t"
            )
            print("PASS default Bedrock API/live/ready/verified TLS/no model request", flush=True)

            collector_command = ["python", "-u", "-m", "jobs.scheduler", "--task", "parking"]
            result = run("collector", [*collector_command, "--once"])
            assert '"status": "success"' in result.stdout
            assert sql("SELECT count(*) FROM parking_facilities") == "1"
            assert sql("SELECT count(*) FROM map_features WHERE parking_id IS NOT NULL") == "1"
            print("PASS real scheduler/child/inherited Secret/TLS/storage/publication", flush=True)

            collector = start(
                "collector", "collector", collector_command, {"TEST_BLOCK_COLLECTION": "1"}
            )
            wait_log(collector, '"status": "test_collection_waiting"')
            assert (
                sql("""
                SELECT count(*) >= 2 AND bool_and(s.ssl)
                FROM pg_stat_activity a JOIN pg_stat_ssl s USING(pid)
                WHERE a.application_name='w2d-collector'
            """)
                == "t"
            )
            interrupted = sql("SELECT id FROM collection_runs WHERE status='running'")
            assert interrupted
            stop(collector, 143)
            output = logs(collector)
            assert output.index('"child_reaped"') < output.index('"lease_released"')
            assert (
                sql("SELECT count(*) FROM pg_stat_activity WHERE application_name='w2d-collector'")
                == "0"
            )
            run("collector", [*collector_command, "--once"])
            assert sql(f"SELECT status FROM collection_runs WHERE id='{interrupted}'") == "failed"
            assert (
                sql(f"SELECT error_code FROM collection_runs WHERE id='{interrupted}'")
                == "collection_interrupted"
            )
            assert sql("SELECT count(*) FROM parking_facilities") == "1"
            assert sql("SELECT count(*) FROM map_features WHERE parking_id IS NOT NULL") == "1"
            print("PASS AWS/TLS/TERM/reap/unlock/restart preserves stored data", flush=True)

            bad_api = start("bad-ca", "api", extra={"APP_DB_SSLROOTCERT": RDS_CA})
            wait_log(bad_api, "Application startup complete")
            health(bad_api, 503)
            stop(bad_api, 0)
            health(api, 200)
            stop(api, 0)
            run("migration", ["alembic", "upgrade", "head"])
            assert sql("SELECT count(*) FROM population_areas") == "121"
            assert sql("SELECT count(*) FROM parking_facilities") == "1"
            print(
                "PASS wrong CA ready=503/live=200/API TERM/migration rerun preserves data",
                flush=True,
            )
        finally:
            for name in reversed(created):
                docker("rm", "-f", name, check=False)
            docker("network", "rm", prefix, check=False)


if __name__ == "__main__":
    main()
