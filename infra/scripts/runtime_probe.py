"""One-shot W3 diagnostic, mounted into the tested image by a ConfigMap.

This is deliberately separate from the application bootstrap: empty W3 application
secrets cannot start the application. Secret payloads and SDK messages never print.
"""

import argparse
import hashlib
import json
import socket
import ssl
import sys
from contextlib import closing
from pathlib import Path

import boto3
import psycopg
from botocore.config import Config
from botocore.exceptions import ClientError


def emit(check, **values):
    print(json.dumps({"check": check, **values}), flush=True)


def run(args):
    session = boto3.Session(region_name=args.region)
    config = Config(
        connect_timeout=3,
        read_timeout=20,
        retries={"total_max_attempts": 2, "mode": "standard"},
        ignore_configured_endpoint_urls=True,
    )
    with closing(session.client("sts", config=config)) as sts:
        identity = sts.get_caller_identity()
    account = args.expected_role.split(":")[4]
    role_name = args.expected_role.rsplit("/", 1)[-1]
    if identity["Account"] != account or not identity["Arn"].startswith(
        f"arn:aws:sts::{account}:assumed-role/{role_name}/"
    ):
        raise RuntimeError("unexpected_pod_identity")
    emit("pod_identity", status="pass", role=role_name)

    with closing(session.client("secretsmanager", config=config)) as secrets:
        if args.own_secret:
            try:
                secrets.get_secret_value(SecretId=args.own_secret, VersionStage="AWSCURRENT")
            except secrets.exceptions.ResourceNotFoundException:
                # Existence and authorization must also be corroborated by AWS/IAM checks.
                emit("own_secret", status="no_current_version", app_settings_validated=False)
            else:
                emit("own_secret", status="readable", app_settings_validated=False)
        if args.forbidden_secret:
            try:
                secrets.get_secret_value(SecretId=args.forbidden_secret)
            except ClientError as error:
                # AccessDenied is not a modeled Secrets Manager exception in all SDK releases.
                if error.response.get("Error", {}).get("Code") not in {
                    "AccessDeniedException",
                    "AccessDenied",
                }:
                    raise RuntimeError("unexpected_secret_error") from None
                emit("forbidden_secret", status="denied")
            else:
                raise RuntimeError("secret_isolation_failed")
        if args.master_secret:
            if not args.db_host or not args.ca_sha256:
                raise RuntimeError("db_host_and_ca_hash_required")
            ca_file = Path("/app/certs/rds/ap-northeast-2-bundle.pem")
            ca_hash = hashlib.sha256(ca_file.read_bytes()).hexdigest()
            if ca_hash != args.ca_sha256:
                raise RuntimeError("unexpected_database_ca")
            emit("database_ca", status="pass", sha256=ca_hash, sslmode="verify-full")
            value = json.loads(
                secrets.get_secret_value(SecretId=args.master_secret)["SecretString"]
            )
            with psycopg.connect(
                host=args.db_host,
                port=args.db_port,
                dbname=args.db_name,
                user=value["username"],
                password=value["password"],
                sslmode="verify-full",
                sslrootcert=str(ca_file),
                gssencmode="disable",
                connect_timeout=5,
                application_name="w3-db-connection-probe",
                options=(
                    "-c default_transaction_read_only=on "
                    "-c statement_timeout=5000 -c lock_timeout=3000 "
                    "-c idle_in_transaction_session_timeout=15000"
                ),
            ) as conn:
                expected_user = value["username"]
                del value
                actual_database, actual_user = conn.execute(
                    "SELECT current_database(), current_user"
                ).fetchone()
                if actual_database != args.db_name or actual_user != expected_user:
                    raise RuntimeError("unexpected_database_identity")
                del expected_user, actual_user
                read_only = conn.execute("SHOW transaction_read_only").fetchone()[0]
                if read_only != "on":
                    raise RuntimeError("database_probe_not_read_only")
                if conn.execute("SELECT 1").fetchone() != (1,):
                    raise RuntimeError("database_query_failed")
                emit(
                    "database_login_query",
                    status="pass",
                    database=actual_database,
                    expected_user_matched=True,
                    transaction_read_only=True,
                    select_one=1,
                )
                row = conn.execute(
                    "SELECT ssl, version, cipher FROM pg_stat_ssl WHERE pid = pg_backend_pid()"
                ).fetchone()
                if not row or row[0] is not True:
                    raise RuntimeError("database_tls_not_verified")
                emit("database_tls", status="pass", protocol=row[1], cipher=row[2])
                version = conn.execute("SHOW server_version").fetchone()[0]
                # rds.force_ssl is checked through the RDS parameter API by the
                # operator; it isn't exposed as a SHOW setting on this instance.
                emit("database_engine", status="pass", postgres=version)
                conn.rollback()
            emit("database_connection_closed", status="pass")
    if args.network:
        for host, port, tls in [
            ("dapi.kakao.com", 443, True),
            ("apis-navi.kakaomobility.com", 443, True),
            ("openapi.seoul.go.kr", 8088, False),
        ]:
            with socket.create_connection((host, port), timeout=5) as sock:
                if tls:
                    with ssl.create_default_context().wrap_socket(sock, server_hostname=host):
                        pass
            emit("external_connection", host=host, port=port, status="pass", authenticated=False)
    if args.bedrock_model:
        with closing(session.client("bedrock-runtime", config=config)) as bedrock:
            response = bedrock.converse(
                modelId=args.bedrock_model,
                messages=[{"role": "user", "content": [{"text": "Reply with exactly OK."}]}],
                inferenceConfig={"maxTokens": 16, "temperature": 0},
            )
        message = response.get("output", {}).get("message", {})
        text = "".join(block.get("text", "") for block in message.get("content", []))
        metadata = response.get("ResponseMetadata", {})
        if (
            metadata.get("HTTPStatusCode") != 200
            or message.get("role") != "assistant"
            or not text.strip()
        ):
            raise RuntimeError("bedrock_empty_response")
        emit(
            "bedrock",
            status="pass",
            model_id=args.bedrock_model,
            http_status=metadata["HTTPStatusCode"],
            stop_reason=response.get("stopReason"),
            reply_matches_ok=text.strip() == "OK",
            max_output_tokens=16,
            usage={
                key: response.get("usage", {}).get(key)
                for key in ("inputTokens", "outputTokens", "totalTokens")
            },
            latency_ms=response.get("metrics", {}).get("latencyMs"),
            sdk_retry_attempts=metadata.get("RetryAttempts"),
            request_id=metadata.get("RequestId"),
        )
    emit("probe_complete", status="pass")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", required=True)
    parser.add_argument("--expected-role", required=True)
    parser.add_argument("--own-secret")
    parser.add_argument("--forbidden-secret")
    parser.add_argument("--master-secret")
    parser.add_argument("--db-host")
    parser.add_argument("--db-name", default="seoul_now")
    parser.add_argument("--db-port", type=int, default=5432)
    parser.add_argument("--ca-sha256")
    parser.add_argument("--bedrock-model")
    parser.add_argument("--network", action="store_true")
    args = parser.parse_args()
    try:
        run(args)
        return 0
    except Exception as error:
        # SDK/DB exception messages can include host/user/request data. Keep stderr fixed.
        emit("probe_failed", error_type=type(error).__name__)
        return 1


if __name__ == "__main__":
    sys.exit(main())
