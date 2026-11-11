"""Isolated Postgres 17/SCRAM activation checks; synthetic inputs, no AWS calls."""

import json
import secrets
import sys
from uuid import uuid4

import psycopg
from sqlalchemy.engine import URL

sys.path.insert(0, "/checks")
import db_bootstrap as foundation  # noqa: E402
import db_credentials as activation  # noqa: E402

ADMIN_PASSWORD = "local-fixture-only"


def connect(database="postgres", username="postgres", password=ADMIN_PASSWORD):
    return psycopg.connect(
        host="db",
        dbname=database,
        user=username,
        password=password,
        autocommit=True,
        connect_timeout=5,
    )


def fingerprints(conn):
    return conn.execute(
        "SELECT rolname, md5(rolpassword) FROM pg_authid WHERE rolname=ANY(%s) ORDER BY rolname",
        (list(foundation.ROLES),),
    ).fetchall()


def main():
    config = {
        "db_name": "w4_login_test",
        "master_username": "postgres",
        "db_host": "db",
        "db_port": 5432,
        "apps": {role: {"version_id": str(uuid4())} for role in activation.ROLES},
    }
    credentials = {}
    for role in activation.ROLES:
        url = URL.create(
            "postgresql+psycopg",
            username=f"seoul_now_{role}",
            password=secrets.token_urlsafe(32),
            host="db",
            port=5432,
            database=config["db_name"],
        )
        payload = {"APP_DATABASE_URL": url.render_as_string(hide_password=False)}
        if role == "api":
            payload.update(
                {"APP_KAKAO_REST_API_KEY": "synthetic", "APP_SEOUL_CITYDATA_API_KEY": "synthetic"}
            )
        credentials[role] = activation.credential_url(json.dumps(payload), role, config)
        invalid = dict(payload)
        invalid["APP_DATABASE_URL"] = url.set(host="wrong-host").render_as_string(
            hide_password=False
        )
        try:
            activation.credential_url(json.dumps(invalid), role, config)
        except activation.CredentialError:
            pass
        else:
            raise AssertionError("foreign_database_target_accepted")
    with connect() as conn:
        conn.execute("CREATE DATABASE w4_login_test TEMPLATE template0")
    with connect(config["db_name"]) as conn:
        foundation.initialize(conn, config["db_name"], "postgres", apply=True)
        inspection = activation.activate(conn, config, credentials)
        assert set(inspection["before"].values()) == {"pending"}
        assert (
            conn.execute(
                "SELECT count(*) FROM pg_authid WHERE rolname=ANY(%s) "
                "AND (rolcanlogin OR rolpassword IS NOT NULL)",
                (list(foundation.ROLES),),
            ).fetchone()[0]
            == 0
        )
        print(json.dumps({"check": "target_validation_and_inspection", "pass": True}), flush=True)

        def changed_version():
            raise activation.CredentialError("simulated_secret_version_change")

        try:
            activation.activate(conn, config, credentials, changed_version, apply=True)
        except activation.CredentialError:
            pass
        else:
            raise AssertionError("version_change_did_not_abort")
        assert (
            conn.execute(
                "SELECT count(*) FROM pg_authid WHERE rolname=ANY(%s) "
                "AND (rolcanlogin OR rolpassword IS NOT NULL)",
                (list(foundation.ROLES),),
            ).fetchone()[0]
            == 0
        )
        print(json.dumps({"check": "password_and_login_atomic_rollback", "pass": True}), flush=True)

        result = activation.activate(conn, config, credentials, apply=True)
        assert len(result["activated"]) == 4
        for role, url in credentials.items():
            with connect(config["db_name"], url.username, url.password) as app:
                assert app.execute("SELECT current_user").fetchone()[0] == url.username
                assert app.execute("SELECT PostGIS_Version()").fetchone()[0]
                assert app.execute(
                    "SELECT has_schema_privilege(current_user,'public','CREATE')"
                ).fetchone()[0] is (role == "migration")
        try:
            with connect(config["db_name"], "seoul_now_api", "deliberately-wrong"):
                raise AssertionError("wrong_password_was_accepted")
        except psycopg.OperationalError as error:
            assert "password authentication failed" in str(error)
        print(
            json.dumps(
                {"check": "four_actual_password_logins_and_wrong_password_denial", "pass": True}
            ),
            flush=True,
        )

        before = fingerprints(conn)
        assert activation.activate(conn, config, credentials, apply=True)["activated"] == []
        assert fingerprints(conn) == before
        config["apps"]["api"]["version_id"] = str(uuid4())
        try:
            activation.activate(conn, config, credentials, apply=True)
        except activation.CredentialError:
            pass
        else:
            raise AssertionError("new_secret_version_silently_rotated_password")
        assert fingerprints(conn) == before
        print(
            json.dumps(
                {"check": "rerun_preserves_passwords_and_blocks_unplanned_rotation", "pass": True}
            ),
            flush=True,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
