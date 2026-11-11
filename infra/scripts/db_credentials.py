"""Activate operator-registered DB credentials, or probe a real application role.

Secret values are fetched only inside EKS. No Secret writes or password output.
This is an initial activation tool, not a general password rotation tool.
"""

import argparse
import asyncio
import hashlib
import json
import os
import sys
from contextlib import closing
from pathlib import Path

import boto3
import psycopg
from botocore.config import Config
from psycopg import sql
from sqlalchemy import text
from sqlalchemy.engine import make_url

from app.core.config import Settings
from app.core.secrets import DB_KEY, parse_secret
from app.db.session import create_engine, read_session_factory, write_session_factory

ROLES = ("api", "collector", "migration", "import")
PENDING_MARKER = "seoul-now:w4-a:v1:awaiting-user-managed-secrets"
ACTIVE_PREFIX = "seoul-now:w4-a:v1:active:"


class CredentialError(Exception):
    """Fixed error codes only: never put input values into these messages."""


def emit(check, **values):
    print(json.dumps({"check": check, **values}), flush=True)


def credential_url(payload, role, config):
    values = parse_secret(payload, role)
    url = make_url(values[DB_KEY])
    if (
        url.drivername != "postgresql+psycopg"
        or url.username != f"seoul_now_{role}"
        or url.host != config["db_host"]
        or url.port != config["db_port"]
        or url.database != config["db_name"]
        or url.query
        or not url.password
        or len(url.password) < 32
        or "\x00" in url.password
    ):
        raise CredentialError("unexpected_database_credential_target")
    return url


def fetch(client, config, role):
    expected = config["apps"][role]
    response = client.get_secret_value(
        SecretId=expected["arn"], VersionId=expected["version_id"], VersionStage="AWSCURRENT"
    )
    if response["VersionId"] != expected["version_id"]:
        raise CredentialError("unexpected_secret_version")
    return response["SecretString"]


def check_identity(client, config, role):
    identity = client.get_caller_identity()
    arn = config["bootstrap_role_arn"] if role == "bootstrap" else config["apps"][role]["role_arn"]
    name = arn.rsplit("/", 1)[-1]
    if identity["Account"] != config["account"] or not identity["Arn"].startswith(
        f"arn:aws:sts::{config['account']}:assumed-role/{name}/"
    ):
        raise CredentialError("unexpected_pod_identity")


def inspect_roles(conn, config):
    if conn.execute("SELECT current_database(), current_user").fetchone() != (
        config["db_name"],
        config["master_username"],
    ):
        raise CredentialError("unexpected_admin_database_identity")
    if conn.execute("SELECT to_regclass('public.alembic_version')").fetchone()[0] is not None:
        raise CredentialError("initial_activation_only")
    if not conn.execute(
        "SELECT EXISTS(SELECT 1 FROM pg_extension WHERE extname='postgis')"
    ).fetchone()[0]:
        raise CredentialError("postgis_missing")
    states = {}
    for role in ROLES:
        row = conn.execute(
            """
            SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolreplication,
                   rolbypassrls, rolinherit, shobj_description(oid, 'pg_authid'),
                   EXISTS(SELECT 1 FROM pg_auth_members m WHERE m.member=r.oid)
            FROM pg_roles r WHERE rolname=%s
        """,
            (f"seoul_now_{role}",),
        ).fetchone()
        if row is None or row[1:7] != (False,) * 6 or row[8]:
            raise CredentialError("unexpected_role_attributes")
        if not row[0] and row[7] == PENDING_MARKER:
            states[role] = "pending"
        elif row[0] and row[7] == ACTIVE_PREFIX + config["apps"][role]["version_id"]:
            states[role] = "already_active"
        else:
            raise CredentialError("role_not_pending_or_active_for_this_version")
    return states


def private_sql_session(conn):
    # Restrict logging only in this transaction; leave the instance configuration intact.
    settings = {
        "log_statement": "none",
        "log_min_duration_statement": "-1",
        "log_min_duration_sample": "-1",
        "log_transaction_sample_rate": "0",
        "log_min_error_statement": "panic",
        "log_parameter_max_length_on_error": "0",
    }
    for key, value in settings.items():
        if conn.execute("SELECT current_setting(%s)", (key,)).fetchone()[0] == value:
            continue
        try:
            conn.execute(
                sql.SQL("SET LOCAL {} TO {}").format(sql.Identifier(key), sql.Literal(value))
            )
        except psycopg.errors.InsufficientPrivilege:
            raise CredentialError("cannot_set_logging_" + key) from None
    # log_parameter_max_length is not settable by the RDS master. Successful
    # statement logging is already disabled above; no password is a bind value.
    audit = conn.execute("SELECT current_setting('pgaudit.log', true)").fetchone()[0]
    if audit and audit not in ("none", "-all"):
        raise CredentialError("audit_logging_requires_review")


def activate(conn, config, credentials, before_commit=lambda: None, apply=False):
    with conn.transaction():
        if not apply:
            conn.execute("SET LOCAL TRANSACTION READ ONLY")
        if not conn.execute(
            "SELECT pg_try_advisory_xact_lock(hashtext('seoul_now:w4-bootstrap'))"
        ).fetchone()[0]:
            raise CredentialError("bootstrap_already_running")
        states = inspect_roles(conn, config)
        private_sql_session(conn)
        if apply:
            for role, state in states.items():
                if state == "already_active":
                    continue
                username = f"seoul_now_{role}"
                # Cleartext passwords never occur in SQL, pg_stat_activity or SQL error text.
                verifier = conn.pgconn.encrypt_password(
                    credentials[role].password.encode("utf-8"),
                    username.encode("utf-8"),
                    b"scram-sha-256",
                ).decode("ascii")
                conn.execute(
                    sql.SQL("ALTER ROLE {} LOGIN PASSWORD {}").format(
                        sql.Identifier(username), sql.Literal(verifier)
                    )
                )
                del verifier
                conn.execute(
                    sql.SQL("COMMENT ON ROLE {} IS {}").format(
                        sql.Identifier(username),
                        sql.Literal(ACTIVE_PREFIX + config["apps"][role]["version_id"]),
                    )
                )
            # Recheck pinned AWSCURRENT versions before committing all four role changes.
            before_commit()
            if set(inspect_roles(conn, config).values()) != {"already_active"}:
                raise CredentialError("activation_verification_failed")
    return {
        "mode": "activate" if apply else "inspect",
        "before": states,
        "activated": [r for r, state in states.items() if state == "pending"] if apply else [],
        "secret_values_written": False,
        "password_sql_format": "scram-sha-256",
        "session_sql_logging_disabled": True,
    }


async def probe_application(config, role):
    settings = Settings()
    if (
        settings.runtime_role != role
        or settings.runtime_env != "aws"
        or not settings.settings_loaded
    ):
        raise CredentialError("application_bootstrap_not_used")
    engine = create_engine(settings)
    try:
        async with read_session_factory(engine)() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT current_database(), current_user, PostGIS_Version(), "
                        "current_setting('transaction_read_only'), "
                        "current_setting('transaction_isolation')"
                    )
                )
            ).one()
            if row[0:2] != (config["db_name"], f"seoul_now_{role}") or row[3:] != (
                "on",
                "repeatable read",
            ):
                raise CredentialError("application_read_session_mismatch")
            tls = (
                await session.execute(
                    text("SELECT ssl, version FROM pg_stat_ssl WHERE pid=pg_backend_pid()")
                )
            ).one()
            if tls[0] is not True:
                raise CredentialError("application_database_tls_missing")
            spatial = await session.scalar(
                text(
                    "SELECT ST_Distance(ST_SetSRID(ST_MakePoint(127,37),4326)::geography, "
                    "ST_SetSRID(ST_MakePoint(127,37),4326)::geography)"
                )
            )
            if spatial != 0:
                raise CredentialError("spatial_query_failed")
            ddl = (
                await session.execute(
                    text(
                        "SELECT has_schema_privilege(current_user,'public','CREATE'), "
                        "has_database_privilege(current_user,current_database(),'CREATE')"
                    )
                )
            ).one()
            if ddl != (role == "migration", role == "migration"):
                raise CredentialError("application_ddl_permissions_mismatch")
        async with write_session_factory(engine)() as session:
            if await session.scalar(text("SHOW transaction_read_only")) != "off":
                raise CredentialError("application_write_session_mismatch")
        return {
            "role": role,
            "db_user": f"seoul_now_{role}",
            "login": True,
            "postgis": row[2],
            "tls": tls[1],
            "read_snapshot": True,
            "spatial_query": True,
            "write_session_read_only": False,
            "ddl_allowed": ddl[0],
            "application_table_writes_tested": False,
        }
    finally:
        await engine.dispose()


def run(args):
    config = json.loads(Path(args.config).read_text())
    ca = Path(config["ca_path"])
    if hashlib.sha256(ca.read_bytes()).hexdigest() != config["ca_sha256"]:
        raise CredentialError("unexpected_database_ca")
    sdk_config = Config(
        connect_timeout=3,
        read_timeout=10,
        retries={"total_max_attempts": 2, "mode": "standard"},
        ignore_configured_endpoint_urls=True,
    )
    session = boto3.Session(region_name=config["region"])
    with closing(session.client("sts", config=sdk_config)) as sts:
        check_identity(sts, config, args.role)
    with closing(session.client("secretsmanager", config=sdk_config)) as client:
        if args.role != "bootstrap":
            values = parse_secret(fetch(client, config, args.role), args.role)
            credential_url(json.dumps(values), args.role, config)
            if any(os.environ.get(key) != value for key, value in values.items()):
                raise CredentialError("application_environment_version_mismatch")
            del values
            result = asyncio.run(probe_application(config, args.role))
            emit(
                "w4_a_login",
                status="pass",
                secret_version=config["apps"][args.role]["version_id"],
                pod_identity=True,
                settings_bootstrap=True,
                **result,
            )
            return
        credentials = {
            role: credential_url(fetch(client, config, role), role, config) for role in ROLES
        }
        master = json.loads(
            client.get_secret_value(
                SecretId=config["master_secret_arn"], VersionStage="AWSCURRENT"
            )["SecretString"]
        )
        if master["username"] != config["master_username"]:
            raise CredentialError("unexpected_master_username")
        with psycopg.connect(
            host=config["db_host"],
            port=config["db_port"],
            dbname=config["db_name"],
            user=master["username"],
            password=master["password"],
            sslmode="verify-full",
            sslrootcert=str(ca),
            gssencmode="disable",
            connect_timeout=5,
            autocommit=True,
            application_name="seoul-now-w4-a-activation",
            options="-c statement_timeout=15000 -c lock_timeout=5000 "
            "-c idle_in_transaction_session_timeout=120000",
        ) as conn:
            del master

            def confirm_versions():
                for role in ROLES:
                    fetch(client, config, role)

            result = activate(conn, config, credentials, confirm_versions, apply=args.apply)
        emit("w4_a_credentials", status="pass", **result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--role", choices=("bootstrap", *ROLES), default="bootstrap")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        if args.apply and args.role != "bootstrap":
            raise CredentialError("only_bootstrap_can_activate")
        run(args)
        return 0
    except Exception as error:
        emit(
            "w4_a_credentials_failed",
            role=args.role,
            error_type=type(error).__name__,
            code=str(error)
            if isinstance(error, CredentialError)
            else "credential_operation_failed",
            sqlstate=error.sqlstate if isinstance(error, psycopg.Error) else None,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
