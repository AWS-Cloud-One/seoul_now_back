"""Explicit check: python -m tests.smoke.shutdown --image IMAGE.

Real signals, process cleanup and DB recovery on an internal Docker network.
Uses synthetic data only and removes its containers/network on completion.
"""

import argparse
import json
import subprocess
import time
from pathlib import Path
from uuid import uuid4, uuid5

from tests.smoke.database_tls import POSTGIS
from tests.smoke.runtime_settings import docker


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    image = parser.parse_args().image
    prefix = f"seoul-now-w2c-{uuid4().hex[:8]}"
    database = prefix + "-db"
    created = []
    fixtures = Path(__file__).resolve().parent / "fixtures"
    docker("network", "create", "--internal", prefix)
    try:
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
            "db",
            "--tmpfs",
            "/var/lib/postgresql/data:rw",
            "-e",
            "POSTGRES_USER=test",
            "-e",
            "POSTGRES_PASSWORD=synthetic-password",
            "-e",
            "POSTGRES_DB=test",
            POSTGIS,
            "postgres",
            "-c",
            "ssl=off",
        )
        created.append(database)
        for _ in range(50):
            if (
                docker(
                    "exec", database, "pg_isready", "-h", "127.0.0.1", "-U", "test", check=False
                ).returncode
                == 0
            ):
                break
            time.sleep(0.2)
        else:
            raise AssertionError(docker("logs", database).stderr)

        def options(run_id=None):
            run_id = run_id or uuid4()
            env = {
                "APP_RUNTIME_ENV": "local",
                "APP_RUNTIME_ROLE": "collector",
                "APP_DATABASE_URL": (
                    "postgresql+psycopg://test:synthetic-password@db/test"
                    f"?application_name=w2c-{run_id}"
                ),
                "APP_DB_SSLMODE": "disable",
                "APP_SEOUL_API_KEY": "synthetic-key",
                "PYTHONPATH": "/checks:/app",
                "TEST_RUN_ID": str(run_id),
            }
            args = [
                "--network",
                prefix,
                "--mount",
                f"type=bind,src={fixtures},dst=/checks,readonly",
            ]
            for key, value in env.items():
                args += ["-e", f"{key}={value}"]
            return args

        docker("run", "--rm", *options(), image, "alembic", "upgrade", "head")

        def sql(query):
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
                query,
            ).stdout.strip()

        def logs(name):
            result = docker("logs", name)
            assert "synthetic-password" not in result.stdout + result.stderr
            return [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]

        def wait_log(name, status, count=1, timeout=12):
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                values = [line for line in logs(name) if line.get("status") == status]
                if len(values) >= count:
                    return values
                state = json.loads(docker("inspect", name).stdout)[0]["State"]
                assert state["Running"], docker("logs", name).stdout + docker("logs", name).stderr
                time.sleep(0.1)
            raise AssertionError(f"{name} did not report {status}")

        def start(scenario, run_id=None):
            name = f"{prefix}-{scenario}-{uuid4().hex[:4]}"
            docker(
                "run",
                "-d",
                "--name",
                name,
                *options(run_id),
                image,
                "python",
                "-u",
                "-m",
                "shutdown_worker",
                scenario,
            )
            created.append(name)
            return name

        def wait_exit(name, expected, timeout=20):
            result = subprocess.run(
                ["docker", "wait", name],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=True,
            )
            assert int(result.stdout) == expected, (
                docker("logs", name).stdout + docker("logs", name).stderr
            )
            return logs(name)

        def assert_sessions_closed(run_id):
            for _ in range(30):
                count = sql(
                    f"SELECT count(*) FROM pg_stat_activity WHERE application_name='w2c-{run_id}'"
                )
                if count == "0":
                    return
                time.sleep(0.1)
            raise AssertionError("job DB sessions survived process exit")

        # Data committed before interruption survives; open transactions roll back;
        # real startup recovery closes the abandoned summary and publishes its parking row.
        def restart_and_check(run_id):
            assert_sessions_closed(run_id)
            assert (
                sql(f"SELECT count(*) FROM parking_facilities WHERE parking_id='{run_id}'") == "1"
            )
            pending = uuid5(run_id, "uncommitted")
            assert (
                sql(f"SELECT count(*) FROM parking_facilities WHERE parking_id='{pending}'") == "0"
            )
            assert sql(f"SELECT status FROM collection_runs WHERE id='{run_id}'") == "running"
            restart = start("restart")
            wait_exit(restart, 0)
            assert sql(
                f"SELECT status||':'||error_code FROM collection_runs WHERE id='{run_id}'"
            ) == ("partial:collection_interrupted")
            assert sql(f"SELECT count(*) FROM map_features WHERE parking_id='{run_id}'") == "1"

        for scenario, ready in (
            ("idle", "success"),
            ("dependency", "child_ready"),
            ("recovery", "recovery_ready"),
        ):
            run_id = uuid4()
            name = start(scenario, run_id)
            wait_log(name, ready)
            docker("kill", "--signal", "TERM", name)
            entries = wait_exit(name, 143)
            assert_sessions_closed(run_id)
            if scenario == "recovery":
                assert not any(x.get("status") == "running" for x in entries)
            if scenario == "dependency":
                assert not any(
                    x.get("task") == "sdot" and x["status"] == "running" for x in entries
                )
            print(f"PASS TERM during {scenario}", flush=True)

        run_id = uuid4()
        name = start("graceful", run_id)
        wait_log(name, "child_ready")
        contender = start("restart")
        assert any(e.get("error_code") == "already_running" for e in wait_exit(contender, 3))
        started = time.monotonic()
        docker("kill", "--signal", "TERM", name)
        docker("kill", "--signal", "TERM", name, check=False)
        entries = wait_exit(name, 143)
        statuses = [entry["status"] for entry in entries]
        assert statuses.index("child_reaped") < statuses.index("lease_released")
        assert statuses.count("shutdown_requested") == 2  # parent and child, each once
        restart_and_check(run_id)
        print(
            "PASS repeat TERM/exclusion/reap before unlock/recovery "
            f"({time.monotonic() - started:.2f}s)",
            flush=True,
        )

        name = start("parallel")
        wait_log(name, "child_ready", count=3)
        started = time.monotonic()
        docker("kill", "--signal", "TERM", name)
        entries = wait_exit(name, 143)
        assert sum(e["status"] == "child_reaped" for e in entries) == 3
        assert time.monotonic() - started < 5
        print("PASS parallel children drained together", flush=True)

        run_id = uuid4()
        name = start("ignore", run_id)
        wait_log(name, "child_ready")
        started = time.monotonic()
        docker("kill", "--signal", "TERM", name)
        entries = wait_exit(name, 143)
        elapsed = time.monotonic() - started
        assert 9 <= elapsed < 18
        assert any(e["status"] == "child_killing" for e in entries)
        assert any(e["status"] == "child_reaped" and e["exit_code"] == -9 for e in entries)
        restart_and_check(run_id)
        print(f"PASS TERM ignored/KILL/reap ({elapsed:.2f}s)", flush=True)

        run_id = uuid4()
        name = start("lease-loss", run_id)
        wait_log(name, "child_ready")
        terminated = sql("""
            SELECT pg_terminate_backend(pid) FROM pg_locks
            WHERE locktype='advisory' AND objsubid=1 AND granted
              AND classid=((hashtext('scheduler:parking')::bigint >> 32) & 4294967295)::oid
              AND objid=(hashtext('scheduler:parking')::bigint & 4294967295)::oid
        """)
        assert terminated == "t"
        started = time.monotonic()
        entries = wait_exit(name, 1)
        assert any(e.get("error_code") == "scheduler_lease_lost" for e in entries)
        assert any(e["status"] == "child_reaped" for e in entries)
        restart_and_check(run_id)
        print(
            f"PASS lost lease cancels child/recovery ({time.monotonic() - started:.2f}s)",
            flush=True,
        )

        run_id = uuid4()
        name = start("graceful", run_id)
        wait_log(name, "child_ready")
        docker("kill", "--signal", "KILL", name)
        entries = wait_exit(name, 137)
        assert not any(e["status"] == "shutdown_completed" for e in entries)
        restart_and_check(run_id)
        print("PASS PID1 KILL/container children gone/DB rollback/restart recovery", flush=True)

        name = start("deadline")
        wait_log(name, "executor_ready")
        started = time.monotonic()
        docker("kill", "--signal", "TERM", name)
        entries = wait_exit(name, 143, timeout=50)
        elapsed = time.monotonic() - started
        assert 44 <= elapsed < 50
        assert "shutdown_deadline_exceeded" in docker("logs", name).stderr
        assert not any(e["status"] == "shutdown_completed" for e in entries)
        print(f"PASS hard deadline includes executor shutdown ({elapsed:.2f}s)", flush=True)

        name = prefix + "-api"
        docker(
            "run",
            "-d",
            "--name",
            name,
            *options(),
            image,
            "uvicorn",
            "shutdown_api:create",
            "--factory",
            "--host",
            "0.0.0.0",
            "--port",
            "8000",
        )
        created.append(name)
        request_code = (
            "import time,urllib.request\n"
            "for i in range(40):\n"
            " try:\n"
            "  urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health/live',timeout=1)\n"
            "  break\n"
            " except OSError: time.sleep(0.1)\n"
            "urllib.request.urlopen('http://127.0.0.1:8000/test-shutdown-ai',timeout=15).read()"
        )
        request = subprocess.Popen(
            ["docker", "exec", name, "python", "-c", request_code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            wait_log(name, "model_thread_started")
            started = time.monotonic()
            docker("kill", "--signal", "TERM", name)
            entries = wait_exit(name, 0)
            request.communicate(timeout=3)
            assert request.returncode == 0
            statuses = [entry["status"] for entry in entries]
            assert statuses.index("model_thread_finished") < statuses.index("model_client_closed")
            print(
                "PASS API waits for mocked Bedrock thread and closes client "
                f"({time.monotonic() - started:.2f}s)",
                flush=True,
            )
        finally:
            if request.poll() is None:
                request.kill()
                request.communicate()
    finally:
        for name in reversed(created):
            docker("rm", "-f", name, check=False)
        docker("network", "rm", prefix, check=False)


if __name__ == "__main__":
    main()
