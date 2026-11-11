"""W4-A foundation only: PostGIS and passwordless, NOLOGIN database roles.

Run inside the bootstrap Pod. The only Secret API call is GetSecretValue for
the existing RDS master. Application secrets and passwords are never written.
Application table grants and credential activation belong to later steps.
"""

import argparse
import hashlib
import json
import sys
from contextlib import closing
from pathlib import Path

import boto3
import psycopg
from botocore.config import Config
from psycopg import sql

ROLES = tuple(f"seoul_now_{name}" for name in ("api", "collector", "migration", "import"))
MIGRATION_ROLE = "seoul_now_migration"
ROLE_MARKER = "seoul-now:w4-a:v1:awaiting-user-managed-secrets"


class BootstrapError(Exception):
    """Only fixed error codes may be put in this exception."""


def emit(check, **values):
    print(json.dumps({"check": check, **values}), flush=True)


def preflight(conn, database, admin):
    identity = conn.execute(
        "SELECT current_database(), current_user, pg_get_userbyid(datdba) "
        "FROM pg_database WHERE datname = current_database()"
    ).fetchone()
    if identity != (database, admin, admin):
        raise BootstrapError("unexpected_database_identity_or_owner")
    if int(conn.execute("SHOW server_version_num").fetchone()[0]) // 10000 != 17:
        raise BootstrapError("unexpected_postgres_major_version")
    relations = conn.execute("""
        SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
          AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
          AND NOT EXISTS (
              SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_class'::regclass
                AND d.objid = c.oid AND d.deptype = 'e'
          )
    """).fetchone()[0]
    if relations:
        raise BootstrapError("nonempty_application_database")
    schema_owner = conn.execute(
        "SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = 'public'"
    ).fetchone()
    if schema_owner not in ((admin,), ("pg_database_owner",)):
        raise BootstrapError("unexpected_public_schema_owner")
    extension = conn.execute("""
        SELECT e.extversion, n.nspname, pg_get_userbyid(e.extowner)
        FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace
        WHERE e.extname = 'postgis'
    """).fetchone()
    # RDS installs extension objects through its internal administrator. Keep
    # AWS ownership intact; neither an app role nor an arbitrary owner is allowed.
    if extension and (extension[1] != "public" or extension[2] not in (admin, "rdsadmin")):
        raise BootstrapError("unexpected_postgis_schema_or_owner")
    available = conn.execute(
        "SELECT default_version FROM pg_available_extensions WHERE name = 'postgis'"
    ).fetchone()
    if not available:
        raise BootstrapError("postgis_not_available")
    existing = []
    for role in ROLES:
        row = conn.execute(
            """
            SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolreplication,
                   rolbypassrls, rolinherit, shobj_description(oid, 'pg_authid'),
                   EXISTS (SELECT 1 FROM pg_auth_members m WHERE m.member = r.oid)
            FROM pg_roles r WHERE rolname = %s
        """,
            (role,),
        ).fetchone()
        if row is not None:
            if row != (False,) * 7 + (ROLE_MARKER, False):
                raise BootstrapError("existing_role_not_pending_bootstrap")
            existing.append(role)
    return {
        "database": database,
        "application_relations": relations,
        "postgis_version": extension[0] if extension else None,
        "postgis_owner": extension[2] if extension else None,
        "postgis_available_version": available[0],
        "existing_pending_roles": existing,
    }


def configure(conn, database, existing):
    if not conn.execute(
        "SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname='postgis')"
    ).fetchone()[0]:
        conn.execute("CREATE EXTENSION postgis WITH SCHEMA public")
    for role in ROLES:
        if role not in existing:
            conn.execute(
                sql.SQL(
                    "CREATE ROLE {} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                    "NOREPLICATION NOBYPASSRLS NOINHERIT PASSWORD NULL"
                ).format(sql.Identifier(role))
            )
            conn.execute(
                sql.SQL("COMMENT ON ROLE {} IS {}").format(
                    sql.Identifier(role), sql.Literal(ROLE_MARKER)
                )
            )
        conn.execute(
            sql.SQL("ALTER ROLE {} IN DATABASE {} SET search_path TO public").format(
                sql.Identifier(role), sql.Identifier(database)
            )
        )
    conn.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(database)))
    conn.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
    for role in ROLES:
        # Reconcile only these pending roles, without touching other named principals.
        conn.execute(
            sql.SQL("REVOKE ALL ON DATABASE {} FROM {}").format(
                sql.Identifier(database), sql.Identifier(role)
            )
        )
        conn.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                sql.Identifier(database), sql.Identifier(role)
            )
        )
        conn.execute(sql.SQL("REVOKE ALL ON SCHEMA public FROM {}").format(sql.Identifier(role)))
        conn.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(role)))
    # 0009 creates legacy_v0008; CREATE on the database permits schemas, not new databases.
    conn.execute(
        sql.SQL("GRANT CREATE ON DATABASE {} TO {}").format(
            sql.Identifier(database), sql.Identifier(MIGRATION_ROLE)
        )
    )
    conn.execute(
        sql.SQL("GRANT CREATE ON SCHEMA public TO {}").format(sql.Identifier(MIGRATION_ROLE))
    )


def ddl_allowed(conn, statement):
    try:
        with conn.transaction():
            conn.execute(statement)
            raise psycopg.Rollback()
        return True
    except psycopg.errors.InsufficientPrivilege:
        return False


def verify(conn, database):
    results = []
    for role in ROLES:
        privileges = conn.execute(
            """
            SELECT has_database_privilege(%s, %s, 'CONNECT'),
                   has_database_privilege(%s, %s, 'CREATE'),
                   has_database_privilege(%s, %s, 'TEMP'),
                   has_schema_privilege(%s, 'public', 'USAGE'),
                   has_schema_privilege(%s, 'public', 'CREATE')
        """,
            (role, database, role, database, role, database, role, role),
        ).fetchone()
        expected_ddl = role == MIGRATION_ROLE
        if privileges != (True, expected_ddl, False, True, expected_ddl):
            raise BootstrapError("unexpected_role_privileges")
        # Temporary SET permission and every probe object roll back at this savepoint.
        # This checks SQL authorization, not password authentication.
        with conn.transaction():
            conn.execute(
                sql.SQL("GRANT {} TO CURRENT_USER WITH SET TRUE").format(sql.Identifier(role))
            )
            conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))
            spatial = conn.execute("""
                SELECT ST_Distance(ST_SetSRID(ST_MakePoint(127, 37),4326)::geography,
                                   ST_SetSRID(ST_MakePoint(127, 37),4326)::geography)
            """).fetchone()[0]
            create_table = ddl_allowed(
                conn, "CREATE TABLE public.w4_a_permission_probe (id integer)"
            )
            create_schema = ddl_allowed(conn, "CREATE SCHEMA w4_a_permission_probe")
            if spatial != 0 or create_table != expected_ddl or create_schema != expected_ddl:
                raise BootstrapError("unexpected_sql_permission_result")
            results.append(
                {
                    "role": role,
                    "login_enabled": False,
                    "connect_privilege": True,
                    "schema_usage": True,
                    "spatial_query": True,
                    "create_table": create_table,
                    "create_schema": create_schema,
                    "temporary_table_privilege": False,
                }
            )
            raise psycopg.Rollback()
    if conn.execute("SELECT to_regclass('public.w4_a_permission_probe')").fetchone()[0] is not None:
        raise BootstrapError("verification_object_not_rolled_back")
    return results


def initialize(conn, database, admin, apply=False):
    with conn.transaction():
        if apply:
            if not conn.execute(
                "SELECT pg_try_advisory_xact_lock(hashtext('seoul_now:w4-bootstrap'))"
            ).fetchone()[0]:
                raise BootstrapError("bootstrap_already_running")
        else:
            conn.execute("SET LOCAL TRANSACTION READ ONLY")
        before = preflight(conn, database, admin)
        if not apply:
            return {"mode": "inspect", "before": before}
        configure(conn, database, before["existing_pending_roles"])
        results = verify(conn, database)
        after = preflight(conn, database, admin)
    # Return only after COMMIT, so a success log cannot precede a failed commit.
    return {"mode": "apply", "before": before, "after": after, "permissions": results}


def run(args):
    settings = json.loads(Path(args.config).read_text())
    config = Config(
        connect_timeout=3,
        read_timeout=10,
        retries={"total_max_attempts": 2, "mode": "standard"},
        ignore_configured_endpoint_urls=True,
    )
    session = boto3.Session(region_name=settings["region"])
    with closing(session.client("sts", config=config)) as sts:
        identity = sts.get_caller_identity()
    role = settings["bootstrap_role_arn"].rsplit("/", 1)[-1]
    if identity["Account"] != settings["account"] or not identity["Arn"].startswith(
        f"arn:aws:sts::{settings['account']}:assumed-role/{role}/"
    ):
        raise BootstrapError("unexpected_bootstrap_identity")
    ca = Path(settings["ca_path"])
    if hashlib.sha256(ca.read_bytes()).hexdigest() != settings["ca_sha256"]:
        raise BootstrapError("unexpected_ca_hash")
    with closing(session.client("secretsmanager", config=config)) as secrets:
        master = json.loads(
            secrets.get_secret_value(
                SecretId=settings["master_secret_arn"], VersionStage="AWSCURRENT"
            )["SecretString"]
        )
    if master["username"] != settings["master_username"]:
        raise BootstrapError("unexpected_master_username")
    with psycopg.connect(
        host=settings["db_host"],
        port=settings["db_port"],
        dbname=settings["db_name"],
        user=master["username"],
        password=master["password"],
        sslmode="verify-full",
        sslrootcert=str(ca),
        gssencmode="disable",
        connect_timeout=5,
        autocommit=True,
        application_name="seoul-now-w4-a-bootstrap",
        options="-c statement_timeout=60000 -c lock_timeout=5000 "
        "-c idle_in_transaction_session_timeout=90000",
    ) as conn:
        del master
        result = initialize(conn, settings["db_name"], settings["master_username"], args.apply)
    emit(
        "w4_a_database",
        status="pass",
        **result,
        application_secrets_written=False,
        application_passwords_generated=False,
        password_login_tested=False,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument(
        "--apply", action="store_true", help="Commit the inspected initial DB setup"
    )
    args = parser.parse_args()
    try:
        run(args)
        return 0
    except Exception as error:
        emit(
            "w4_a_failed",
            error_type=type(error).__name__,
            code=str(error) if isinstance(error, BootstrapError) else "bootstrap_operation_failed",
            sqlstate=error.sqlstate if isinstance(error, psycopg.Error) else None,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
