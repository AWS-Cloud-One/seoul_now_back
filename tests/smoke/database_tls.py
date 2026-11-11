"""Explicit integration check: python -m tests.smoke.database_tls --image IMAGE.

Creates disposable PostGIS servers on an internal Docker network, with a temporary
test CA. AWS secrets are stubbed. No host ports, AWS calls, or existing DBs are used.
"""

import argparse
import json
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from tests.smoke.runtime_settings import HOOK, docker

POSTGIS = (
    "postgis/postgis:17-3.6@sha256:eeead53aa76f9219eac3af490e00484a7dd604dbcbc3ed2ae0e96668986f16f3"
)
RDS_CA = "/app/certs/rds/ap-northeast-2-bundle.pem"
PROBE = """
import asyncio, json, os, sys
from sqlalchemy import text
from app.core.config import Settings
from app.db.session import create_engine, read_session_factory, write_session_factory
from jobs.runtime import run_cli

async def probe():
    engine = create_engine(Settings())
    try:
        async with engine.connect() as connection:
            encrypted = await connection.scalar(text(
                "SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()"))
            assert encrypted is (os.environ["EXPECT_TLS"] == "1")
            assert await connection.scalar(text("SELECT PostGIS_Version()"))
        async with read_session_factory(engine)() as session:
            assert await session.scalar(text("SHOW transaction_read_only")) == "on"
            assert await session.scalar(text("SHOW transaction_isolation")) == "repeatable read"
        async with write_session_factory(engine)() as session:
            assert await session.scalar(text("SHOW transaction_read_only")) == "off"
        print(json.dumps({"tls": encrypted, "role": Settings().runtime_role}))
        return 0
    finally:
        await engine.dispose()

if __name__ == "__main__":
    run_cli(probe)
"""


def certificates(directory):
    config = directory / "openssl.cnf"
    config.write_text("""
[req]
distinguished_name=dn
[dn]
[ca]
basicConstraints=critical,CA:TRUE
keyUsage=critical,keyCertSign,cRLSign
[server]
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=DNS:db-tls
""")
    commands = [
        [
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "2",
            "-subj",
            "/CN=W2B-Test-CA",
            "-config",
            "openssl.cnf",
            "-extensions",
            "ca",
            "-keyout",
            "ca.key",
            "-out",
            "ca.pem",
        ],
        [
            "req",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-subj",
            "/CN=db-tls",
            "-config",
            "openssl.cnf",
            "-keyout",
            "server.key",
            "-out",
            "server.csr",
        ],
        [
            "x509",
            "-req",
            "-in",
            "server.csr",
            "-CA",
            "ca.pem",
            "-CAkey",
            "ca.key",
            "-set_serial",
            "1",
            "-days",
            "2",
            "-extfile",
            "openssl.cnf",
            "-extensions",
            "server",
            "-out",
            "server.pem",
        ],
    ]
    for args in commands:
        subprocess.run(
            ["openssl", *args], cwd=directory, check=True, capture_output=True, timeout=15
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    image = parser.parse_args().image
    prefix = f"seoul-now-w2b-{uuid.uuid4().hex[:8]}"
    created = []
    with tempfile.TemporaryDirectory(prefix="seoul-now-w2b-") as temp:
        directory = Path(temp)
        directory.chmod(0o755)
        certificates(directory)
        (directory / "probe.py").write_text(PROBE)
        # A test-only startup hook provides synthetic role secrets to the real ENTRYPOINT.
        hook = HOOK.replace(
            '"postgresql+psycopg://test:synthetic-password@db.example/test"',
            '"postgresql+psycopg://test:synthetic-password@"'
            ' + os.environ["TEST_DB_HOST"] + "/test"',
        )
        (directory / "sitecustomize.py").write_text(hook)
        mount = f"type=bind,src={directory},dst=/checks,readonly"
        docker("network", "create", "--internal", prefix)
        try:
            for tls in (True, False):
                host = "db-tls" if tls else "db-plain"
                name = f"{prefix}-{host}"
                command = ["postgres", "-c", "ssl=off"]
                if tls:
                    command = [
                        "bash",
                        "-c",
                        "install -o postgres -g postgres -m 600 "
                        "/checks/server.key /tmp/server.key; "
                        "exec docker-entrypoint.sh postgres -c ssl=on "
                        "-c ssl_cert_file=/checks/server.pem -c ssl_key_file=/tmp/server.key",
                    ]
                aliases = ["--network-alias", host]
                if tls:
                    aliases += ["--network-alias", "db-wrong"]
                docker(
                    "run",
                    "-d",
                    "--name",
                    name,
                    "--platform",
                    "linux/amd64",
                    "--network",
                    prefix,
                    *aliases,
                    "--mount",
                    mount,
                    "--tmpfs",
                    "/var/lib/postgresql/data:rw",
                    "-e",
                    "POSTGRES_USER=test",
                    "-e",
                    "POSTGRES_PASSWORD=synthetic-password",
                    "-e",
                    "POSTGRES_DB=test",
                    POSTGIS,
                    *command,
                )
                created.append(name)
                for _ in range(50):
                    ready = docker(
                        "exec", name, "pg_isready", "-h", "127.0.0.1", "-U", "test", check=False
                    )
                    if ready.returncode == 0:
                        break
                    time.sleep(0.2)
                else:
                    raise AssertionError(docker("logs", name).stderr)

            def options(role="migration", host="db-tls", ca="/checks/ca.pem", local=False):
                environment = {
                    "PYTHONPATH": "/checks:/app",
                    "APP_RUNTIME_ROLE": role,
                    "APP_RUNTIME_ENV": "local" if local else "aws",
                    "TEST_DB_HOST": host,
                    "EXPECT_TLS": "0" if local else "1",
                    "APP_DB_SSLMODE": "disable" if local else "verify-full",
                }
                if local:
                    environment["APP_DATABASE_URL"] = (
                        "postgresql+psycopg://test:synthetic-password@db-plain/test"
                    )
                else:
                    environment.update(
                        APP_DB_SSLROOTCERT=ca,
                        AWS_REGION="ap-northeast-2",
                        APP_SETTINGS_SECRET_ID=(
                            "arn:aws:secretsmanager:ap-northeast-2:123456789012:"
                            f"secret:seoul-now/lab/{role}-a1B2c3"
                        ),
                        # These must never downgrade explicit application connection args.
                        PGSSLMODE="disable",
                        PGSSLROOTCERT="/wrong.pem",
                        PGGSSENCMODE="require",
                    )
                args = ["--network", prefix, "--mount", mount]
                for key, value in environment.items():
                    args += ["-e", f"{key}={value}"]
                return args

            def run(command, expected=0, **kwargs):
                result = docker("run", "--rm", *options(**kwargs), image, *command, check=False)
                assert result.returncode == expected, result.stdout + result.stderr
                assert "synthetic-password" not in result.stdout + result.stderr
                return result

            # Apply real migrations to an empty, isolated TLS DB and compare schema.
            run(["alembic", "upgrade", "head"])
            run(["alembic", "check"])
            print("PASS TLS Alembic upgrade head/schema check", flush=True)
            for role in ("api", "collector", "import", "migration"):
                result = run(["python", "/checks/probe.py"], role=role)
                assert json.loads(result.stdout) == {"tls": True, "role": role}
                print(f"PASS {role}/pg_stat_ssl=true/session policy", flush=True)

            # The real API readiness route must connect successfully over verified TLS.
            name = f"{prefix}-api"
            docker("run", "-d", "--name", name, *options(role="api"), image)
            created.append(name)
            for _ in range(40):
                health = docker(
                    "exec",
                    name,
                    "python",
                    "-c",
                    "import urllib.request; assert urllib.request.urlopen("
                    "'http://127.0.0.1:8000/api/v1/health/ready', timeout=3).status == 200",
                    check=False,
                )
                if health.returncode == 0:
                    break
                time.sleep(0.2)
            else:
                raise AssertionError(docker("logs", name).stderr)
            docker("stop", "--time", "5", name)
            print("PASS API readiness over TLS", flush=True)

            for label, kwargs, cause in (
                ("wrong CA", {"ca": RDS_CA}, "certificate verify failed"),
                ("wrong host", {"host": "db-wrong"}, "does not match host name"),
                ("non-TLS server", {"host": "db-plain"}, "server does not support SSL"),
                ("missing CA", {"ca": "/checks/missing.pem"}, "invalid_application_settings"),
            ):
                result = run(["python", "/checks/probe.py"], expected=1, role="collector", **kwargs)
                assert "error_code" in result.stdout + result.stderr
                failure = run(["alembic", "current"], expected=1, **kwargs)
                assert cause in failure.stderr, failure.stderr
                print(f"PASS reject {label}/job and Alembic", flush=True)

            # Hold the existing session lock and verify another Alembic invocation is rejected.
            locker = f"{prefix}-lock"
            lock_code = (
                "import asyncio; from pathlib import Path; from sqlalchemy import text; "
                "from app.core.config import Settings; from app.db.session import create_engine\n"
                "async def hold():\n"
                " e=create_engine(Settings())\n"
                " async with e.connect() as c:\n"
                '  await c.execute(text("SELECT pg_advisory_lock('
                "hashtext('seoul_now:alembic'))\"))\n"
                "  await c.commit()\n"
                "  Path('/tmp/locked').touch()\n"
                "  await asyncio.sleep(60)\n"
                "asyncio.run(hold())"
            )
            docker("run", "-d", "--name", locker, *options(), image, "python", "-c", lock_code)
            created.append(locker)
            for _ in range(30):
                if docker("exec", locker, "test", "-f", "/tmp/locked", check=False).returncode == 0:
                    break
                time.sleep(0.2)
            else:
                raise AssertionError(docker("logs", locker).stderr)
            locked = run(["alembic", "upgrade", "head"], expected=1)
            assert "Another migration is running" in locked.stderr
            docker("stop", "--time", "1", locker)
            run(["alembic", "upgrade", "head"])
            print("PASS migration lock exclusion and release", flush=True)

            run(["alembic", "upgrade", "head"], local=True)
            result = run(["python", "/checks/probe.py"], local=True)
            assert json.loads(result.stdout)["tls"] is False
            print("PASS local non-TLS migration/connection", flush=True)
        finally:
            for name in reversed(created):
                docker("rm", "-f", name, check=False)
            docker("network", "rm", prefix, check=False)


if __name__ == "__main__":
    main()
