"""Run only inside a disposable Docker network; never accepts AWS credentials.

Mount this file and infra/scripts/db_bootstrap.py into /checks, run with the
existing production image and a fresh PostgreSQL 17/PostGIS server named db.
"""

import json
import sys

import psycopg
from psycopg import sql

sys.path.insert(0, "/checks")
import db_bootstrap as bootstrap  # noqa: E402


def connect(database="postgres"):
    return psycopg.connect(host="db", dbname=database, user="postgres", autocommit=True)


def rejected(conn, expected):
    try:
        bootstrap.initialize(conn, "w4_test", "postgres", apply=True)
    except bootstrap.BootstrapError as error:
        assert str(error) == expected
    else:
        raise AssertionError("unsafe_bootstrap_was_not_rejected")


def main():
    with connect() as conn:
        conn.execute("CREATE DATABASE w4_test TEMPLATE template0")
    with connect("w4_test") as conn:
        inspected = bootstrap.initialize(conn, "w4_test", "postgres")
        assert inspected["before"]["postgis_version"] is None
        assert inspected["before"]["existing_pending_roles"] == []
        assert not conn.execute("SELECT 1 FROM pg_extension WHERE extname='postgis'").fetchone()
        print(json.dumps({"check": "inspect_is_read_only", "pass": True}), flush=True)

        # A failure after configuration must roll back extension, roles and grants together.
        original_verify = bootstrap.verify

        def fail_verification(*_args):
            raise bootstrap.BootstrapError("injected_verification_failure")

        bootstrap.verify = fail_verification
        try:
            rejected(conn, "injected_verification_failure")
        finally:
            bootstrap.verify = original_verify
        assert not conn.execute("SELECT 1 FROM pg_extension WHERE extname='postgis'").fetchone()
        assert (
            conn.execute(
                "SELECT count(*) FROM pg_roles WHERE rolname=ANY(%s)", (list(bootstrap.ROLES),)
            ).fetchone()[0]
            == 0
        )
        print(json.dumps({"check": "atomic_rollback", "pass": True}), flush=True)

        first = bootstrap.initialize(conn, "w4_test", "postgres", apply=True)
        assert len(first["permissions"]) == 4
        assert first["after"]["application_relations"] == 0
        assert (
            conn.execute(
                "SELECT count(*) FROM pg_authid WHERE rolname=ANY(%s) "
                "AND (rolcanlogin OR rolpassword IS NOT NULL)",
                (list(bootstrap.ROLES),),
            ).fetchone()[0]
            == 0
        )
        original_oids = conn.execute(
            "SELECT oid FROM pg_roles WHERE rolname=ANY(%s) ORDER BY oid", (list(bootstrap.ROLES),)
        ).fetchall()
        again = bootstrap.initialize(conn, "w4_test", "postgres", apply=True)
        assert first["after"] == again["after"]
        assert (
            original_oids
            == conn.execute(
                "SELECT oid FROM pg_roles WHERE rolname=ANY(%s) ORDER BY oid",
                (list(bootstrap.ROLES),),
            ).fetchall()
        )
        print(
            json.dumps(
                {
                    "check": "roles_privileges_spatial_and_rerun",
                    "pass": True,
                    "permissions": first["permissions"],
                }
            ),
            flush=True,
        )

        conn.execute("COMMENT ON ROLE seoul_now_api IS 'unmanaged'")
        rejected(conn, "existing_role_not_pending_bootstrap")
        conn.execute(
            sql.SQL("COMMENT ON ROLE seoul_now_api IS {}").format(
                sql.Literal(bootstrap.ROLE_MARKER)
            )
        )
        conn.execute("ALTER ROLE seoul_now_api LOGIN")
        rejected(conn, "existing_role_not_pending_bootstrap")
        assert conn.execute(
            "SELECT rolcanlogin FROM pg_roles WHERE rolname='seoul_now_api'"
        ).fetchone()[0]
        conn.execute("ALTER ROLE seoul_now_api NOLOGIN")
        print(
            json.dumps({"check": "unmanaged_and_activated_roles_preserved", "pass": True}),
            flush=True,
        )

        conn.execute("CREATE TABLE public.existing_data (id integer)")
        conn.execute("INSERT INTO public.existing_data VALUES (42)")
        rejected(conn, "nonempty_application_database")
        assert conn.execute("SELECT id FROM public.existing_data").fetchone() == (42,)
        print(json.dumps({"check": "existing_data_preserved", "pass": True}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
