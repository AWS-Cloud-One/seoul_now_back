"""W4 one-shot migration, permissions and data preparation checks inside EKS."""

import argparse
import json
import sys
from uuid import uuid4

import psycopg
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from psycopg import sql
from sqlalchemy.engine import make_url

from app.core.config import Settings
from app.db.connection import connection_args
from app.models import Base

TABLES = set(Base.metadata.tables)
# In-flight API state (0015/0016) holds user conditions and origins, like chat data:
# only the API reads and writes it.
API_STATE = {"query_cursors", "read_replays", "trip_progress", "trip_requests", "origin_contexts"}
PRIVATE = {name for name in TABLES if name.startswith(("chat_", "recommendation_"))} | API_STATE
COLLECTION = {"data_sources", "collection_runs", "collection_rejection_reasons"}
MAP = {name for name in TABLES if name.startswith("map_")}
IMPORT_WRITES = (
    COLLECTION
    | MAP
    | {
        "population_areas",
        "road_links",
        "road_link_mappings",
        "road_area_mappings",
        "pedestrian_sensors",
    }
)
COLLECTOR_WRITES = (
    COLLECTION
    | MAP
    | {
        "collection_cursors",
        "collection_cursor_pages",
        "administrative_areas",
        "region_boundaries",
        "road_area_mappings",
        "pedestrian_sensor_latest",
        "population_latest",
        "population_attempt_latest",
        "road_traffic_latest",
        "road_traffic_attempt_latest",
    }
    | {name for name in TABLES if name.startswith("parking_")}
)
# Name enrichment must not replace geometry or change road identity/provenance.
# The ORM updates updated_at alongside name; column UPDATE also permits row locks.
COLUMN_UPDATES = {("collector", "road_links"): {"name", "updated_at"}}


def rights(role, table):
    if role == "migration":
        return {"SELECT", "INSERT", "UPDATE", "DELETE"}
    if table == "alembic_version" or (role != "api" and table in PRIVATE):
        return set()
    result = {"SELECT"}
    writes = {"api": PRIVATE, "collector": COLLECTOR_WRITES, "import": IMPORT_WRITES}[role]
    if table in writes:
        result |= {"INSERT", "UPDATE", "DELETE"}
    # Row locks require UPDATE. Import also updates the road/boundary version link.
    if (role, table) in {("collector", "pedestrian_sensors"), ("import", "administrative_areas")}:
        result.add("UPDATE")
    return result


def apply_table_grants(conn, role, table, username):
    target = sql.Identifier("public", table)
    user = sql.Identifier(username)
    # Table REVOKE also clears existing column grants before restoring the policy.
    conn.execute(sql.SQL("REVOKE ALL ON TABLE {} FROM {}").format(target, user))
    allowed = rights(role, table)
    if allowed:
        conn.execute(
            sql.SQL("GRANT {} ON TABLE {} TO {}").format(
                sql.SQL(", ").join(map(sql.SQL, sorted(allowed))), target, user
            )
        )
    columns = COLUMN_UPDATES.get((role, table), set())
    if columns:
        conn.execute(
            sql.SQL("GRANT UPDATE ({}) ON TABLE {} TO {}").format(
                sql.SQL(", ").join(map(sql.Identifier, sorted(columns))), target, user
            )
        )


def grants(settings):
    if settings.runtime_role != "migration":
        raise RuntimeError("migration_role_required")
    tables = TABLES | {"alembic_version"}
    with connect(settings) as conn, conn.transaction():
        actual = {
            r[0]
            for r in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname='public' "
                "AND tableowner=current_user"
            )
        }
        if actual != tables:
            raise RuntimeError("unexpected_owned_tables")
        for role in ("api", "collector", "import"):
            for table in sorted(tables):
                apply_table_grants(conn, role, table, "seoul_now_" + role)
        # Current schema has UUID/text identities and no application sequences.
        if conn.execute(
            "SELECT count(*) FROM pg_sequences WHERE schemaname='public' "
            "AND sequenceowner=current_user"
        ).fetchone()[0]:
            raise RuntimeError("sequence_grants_require_review")
    emit(
        "w4_table_grants",
        status="pass",
        tables=len(tables),
        policy={
            role: {t: sorted(rights(role, t)) for t in sorted(tables)}
            for role in ("api", "collector", "import")
        },
        column_updates={
            f"{role}.{table}": sorted(columns) for (role, table), columns in COLUMN_UPDATES.items()
        },
    )


def probe(settings):
    role = settings.runtime_role
    passed = denied = 0
    with connect(settings) as conn:
        for table in sorted(TABLES | {"alembic_version"}):
            target = sql.Identifier("public", table)
            # Use model columns without reading inaccessible information_schema rows.
            column = (
                "version_num"
                if table == "alembic_version"
                else next(iter(Base.metadata.tables[table].columns)).name
            )
            statements = {
                "SELECT": sql.SQL("SELECT * FROM {} WHERE false").format(target),
                "INSERT": sql.SQL("INSERT INTO {} SELECT * FROM {} WHERE false").format(
                    target, target
                ),
                "UPDATE": sql.SQL("UPDATE {} SET {}={} WHERE false").format(
                    target, sql.Identifier(column), sql.Identifier(column)
                ),
                "DELETE": sql.SQL("DELETE FROM {} WHERE false").format(target),
            }
            if any(t == table for _, t in COLUMN_UPDATES):
                for field in Base.metadata.tables[table].columns:
                    statements[f"UPDATE:{field.name}"] = sql.SQL(
                        "UPDATE {} SET {}={} WHERE false"
                    ).format(target, sql.Identifier(field.name), sql.Identifier(field.name))
            for operation, statement in statements.items():
                privilege, _, field = operation.partition(":")
                expected = privilege in rights(role, table) or (
                    privilege == "UPDATE" and field in COLUMN_UPDATES.get((role, table), set())
                )
                try:
                    with conn.transaction(force_rollback=True):
                        conn.execute(statement)
                except psycopg.errors.InsufficientPrivilege:
                    if expected:
                        emit("unexpected_denial", role=role, table=table, operation=operation)
                        raise
                    denied += 1
                else:
                    if not expected:
                        raise RuntimeError("unexpected_table_permission")
                    passed += 1
        try:
            with conn.transaction(force_rollback=True):
                conn.execute("CREATE TABLE public.w4_permission_probe (id integer)")
                conn.execute("ALTER TABLE public.w4_permission_probe ADD COLUMN note text")
        except psycopg.errors.InsufficientPrivilege:
            if role == "migration":
                raise
        else:
            if role != "migration":
                raise RuntimeError("unexpected_ddl_permission")
        # Exercise real rows, while rolling the entire fixture back.
        with conn.transaction(force_rollback=True):
            if role == "api":
                key = uuid4()
                conn.execute(
                    "INSERT INTO chat_sessions(trip_id,expires_at) "
                    "VALUES (%s,now()+interval '1 hour')",
                    (key,),
                )
                conn.execute(
                    "INSERT INTO chat_condition_versions(trip_id,version) VALUES (%s,0)", (key,)
                )
                conn.execute("SET CONSTRAINTS ALL IMMEDIATE")
                conn.execute(
                    "UPDATE chat_sessions SET expires_at=expires_at+interval '1 minute' "
                    "WHERE trip_id=%s",
                    (key,),
                )
                conn.execute("SET CONSTRAINTS ALL DEFERRED")
                conn.execute("DELETE FROM chat_condition_versions WHERE trip_id=%s", (key,))
                conn.execute("DELETE FROM chat_sessions WHERE trip_id=%s", (key,))
            else:
                key = "w4_probe_" + uuid4().hex
                conn.execute(
                    "INSERT INTO data_sources(source_code,display_name,provider_name) "
                    "VALUES (%s,'W4 probe','W4 probe')",
                    (key,),
                )
                conn.execute(
                    "UPDATE data_sources SET display_name='W4 updated' WHERE source_code=%s", (key,)
                )
                conn.execute("DELETE FROM data_sources WHERE source_code=%s", (key,))
    emit(
        "w4_table_permission_probe",
        status="pass",
        role=role,
        allowed=passed,
        denied=denied,
        ddl_allowed=role == "migration",
        real_row_insert_update_delete="pass_rolled_back",
    )


def emit(check, **values):
    print(json.dumps({"check": check, **values}), flush=True)


def connect(settings):
    url = make_url(settings.database_url.get_secret_value())
    return psycopg.connect(
        host=url.host,
        port=url.port,
        dbname=url.database,
        user=url.username,
        password=url.password,
        **connection_args(settings),
        autocommit=True,
        application_name="seoul-now-w4-preparation",
    )


def migrate(settings):
    if settings.runtime_role != "migration":
        raise RuntimeError("migration_role_required")
    cfg = Config("alembic.ini")
    head = ScriptDirectory.from_config(cfg).get_current_head()
    command.upgrade(cfg, "head")
    # A second upgrade must be a no-op at the same head shipped in this image.
    command.upgrade(cfg, "head")
    command.check(cfg)
    with connect(settings) as conn:
        revision = conn.execute("SELECT version_num FROM alembic_version").fetchall()
        if revision != [(head,)]:
            raise RuntimeError("unexpected_revision")
        tables = conn.execute("""
            SELECT tablename FROM pg_tables WHERE schemaname='public'
            AND tableowner=current_user ORDER BY tablename
        """).fetchall()
        emit(
            "w4_migration",
            status="pass",
            revision=head,
            repeat_upgrade="no_change",
            schema_comparison="no_changes",
            owner="seoul_now_migration",
            owned_tables=[row[0] for row in tables],
        )


def revision(settings):
    """Report the database revision and the migrations shipped in this image, read-only.

    Runs with the image being deployed (possibly an older one), before anything
    changes; infra/cicd/db_compat.py decides from this whether the deploy may proceed.
    """
    if settings.runtime_role != "migration":
        raise RuntimeError("migration_role_required")
    scripts = ScriptDirectory.from_config(Config("alembic.ini"))
    with connect(settings) as conn:
        conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
        rows = conn.execute("SELECT version_num FROM alembic_version").fetchall()
    if len(rows) != 1:
        raise RuntimeError("unexpected_revision")
    emit(
        "w4_revision",
        status="pass",
        db_revision=rows[0][0],
        image_head=scripts.get_current_head(),
        image_revisions=sorted(item.revision for item in scripts.walk_revisions()),
    )


def chat_preflight(settings):
    """Read-only readiness for the V2 API role; never collect or alter reference data."""
    if settings.runtime_role != "api":
        raise RuntimeError("api_role_required")
    with connect(settings) as conn:
        conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
        # Required writer tables are checked by probe(); these reads also verify the
        # latest history/lease columns, without printing any conversations or tokens.
        conn.execute("SELECT sequence, question, implementation_version FROM chat_runs LIMIT 0")
        conn.execute("SELECT used FROM chat_usage_buckets LIMIT 0")
    chat_reference(settings)


def chat_reference(settings):
    """Safe to run before V2 DDL, while the V1 API is still serving."""
    if settings.runtime_role != "api":
        raise RuntimeError("api_role_required")
    with connect(settings) as conn:
        conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
        queries = {
            "seoul_boundary": (
                "SELECT count(*) FROM region_boundaries WHERE "
                "code_system='sgis' AND region_code='11' AND is_active "
                "AND processed_at IS NOT NULL"
            ),
            "districts": (
                "SELECT count(*) FROM region_boundaries WHERE code_system='sgis' "
                "AND region_code LIKE '11%' AND length(region_code)=5 AND is_active "
                "AND processed_at IS NOT NULL"
            ),
            "dongs": (
                "SELECT count(*) FROM administrative_areas WHERE code_system='sgis' "
                "AND sigungu_code LIKE '11%' AND source='sgis_areas' AND is_active "
                "AND processed_at IS NOT NULL"
            ),
            "population_areas": (
                "SELECT count(*) FROM population_areas WHERE is_active "
                "AND source='citydata_place_boundaries' AND source_version IS NOT NULL "
                "AND processed_at IS NOT NULL"
            ),
        }
        counts = {name: conn.execute(query).fetchone()[0] for name, query in queries.items()}
    minimum = {"seoul_boundary": 1, "districts": 25, "dongs": 400, "population_areas": 121}
    missing = [name for name, count in counts.items() if count < minimum[name]]
    emit("chat_v2_preflight", status="fail" if missing else "pass", counts=counts, missing=missing)
    if missing:
        raise RuntimeError("chat_reference_data_incomplete")


OPERATIONS = {
    "migrate": migrate,
    "grants": grants,
    "probe": probe,
    "revision": revision,
    "chat-preflight": chat_preflight,
    "chat-reference": chat_reference,
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=tuple(OPERATIONS))
    args = parser.parse_args()
    try:
        settings = Settings()
        OPERATIONS[args.operation](settings)
        return 0
    except Exception as error:
        emit(
            "w4_operation_failed",
            operation=args.operation,
            error_type=type(error).__name__,
            sqlstate=error.sqlstate if isinstance(error, psycopg.Error) else None,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
