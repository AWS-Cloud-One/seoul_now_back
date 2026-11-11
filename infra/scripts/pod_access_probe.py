"""Read-only W3 Pod Identity and access-boundary checks.

Mount this script and metadata-only config into the existing backend image.
Never print Secret payloads, credentials, SDK error messages, or tracebacks.
Empty Secret versions must be corroborated by operator metadata and IAM checks.
"""

import argparse
import json
import os
import sys
from contextlib import ExitStack, closing

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


def emit(probe_id, role, check, **values):
    print(json.dumps({"probe_id": probe_id, "role": role, "check": check, **values}), flush=True)


def check_call(probe_id, role, check, operation, parameters, expected, denied_codes):
    """Record only the outcome; negative tests intentionally inspect AWS error codes."""
    try:
        response = operation(**parameters)
    except ClientError as error:
        # AccessDenied/UnauthorizedOperation are not modeled exceptions in every SDK.
        # Comparing codes is part of the test assertion, not an application fallback.
        code = error.response.get("Error", {}).get("Code", "Unknown")
        if code in denied_codes:
            outcome = "denied"
        elif code == "ResourceNotFoundException":
            outcome = "no_current_version"
        else:
            outcome = "unexpected_aws_error"
        passed = outcome == expected
        emit(probe_id, role, check, expected=expected, outcome=outcome, passed=passed, error_code=code)
    else:
        # A successful Secret read is discarded inside this Pod without parsing it.
        response.clear()
        passed = expected == "allowed"
        emit(probe_id, role, check, expected=expected, outcome="allowed", passed=passed)
    return passed


def run(config, role):
    item = config["roles"][role]
    probe_id = config["probe_id"]
    if any(os.environ.get(k) for k in (
        "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
        "AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_PROFILE",
    )):
        raise RuntimeError("unexpected_credential_override")
    if not all(os.environ.get(k) for k in (
        "AWS_CONTAINER_CREDENTIALS_FULL_URI", "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
    )):
        raise RuntimeError("pod_identity_not_injected")

    session = boto3.Session(region_name=config["region"])
    sdk_config = Config(
        connect_timeout=3,
        read_timeout=15,
        retries={"total_max_attempts": 2, "mode": "standard"},
        ignore_configured_endpoint_urls=True,
    )
    credentials = session.get_credentials()
    if credentials is None or credentials.method != "container-role":
        raise RuntimeError("unexpected_credential_provider")
    results = []
    with ExitStack() as stack:
        def client(service):
            return stack.enter_context(closing(session.client(service, config=sdk_config)))

        identity = client("sts").get_caller_identity()
        role_name = item["role_arn"].rsplit("/", 1)[-1]
        expected_prefix = f"arn:aws:sts::{config['account_id']}:assumed-role/{role_name}/"
        if identity["Account"] != config["account_id"] or not identity["Arn"].startswith(expected_prefix):
            raise RuntimeError("unexpected_pod_role")
        emit(probe_id, role, "identity", passed=True, arn=identity["Arn"], provider=credentials.method)
        secrets = client("secretsmanager")
        for label, metadata in config["secrets"].items():
            if label not in item["allowed_secrets"]:
                expected = "denied"
            elif metadata["has_current_version"]:
                expected = "allowed"
            else:
                expected = "no_current_version"
            results.append(check_call(
                probe_id, role, f"secret/{label}", secrets.get_secret_value,
                {"SecretId": metadata["arn"], "VersionStage": "AWSCURRENT"}, expected,
                {"AccessDenied", "AccessDeniedException"},
            ))

        if role == "import":
            s3 = client("s3")
            for prefix, expected in (("reference/", "allowed"), ("outside-reference/", "denied")):
                results.append(check_call(
                    probe_id, role, f"s3/list/{prefix}", s3.list_objects_v2,
                    {"Bucket": config["data_bucket"], "Prefix": prefix, "MaxKeys": 1},
                    expected, {"AccessDenied", "AccessDeniedException"},
                ))
        if role in ("alb", "bootstrap"):
            results.append(check_call(
                probe_id, role, "ec2/describe_vpc", client("ec2").describe_vpcs,
                {"VpcIds": [config["vpc_id"]]}, "allowed" if role == "alb" else "denied",
                {"UnauthorizedOperation", "AccessDenied", "AccessDeniedException"},
            ))
        if role == "alb":
            results.append(check_call(
                probe_id, role, "elbv2/describe_load_balancers", client("elbv2").describe_load_balancers,
                {"PageSize": 1}, "allowed", {"AccessDenied", "AccessDeniedException"},
            ))
        if role == "logs":
            logs = client("logs")
            for label, expected in (("application_log_group", "allowed"), ("forbidden_log_group", "denied")):
                results.append(check_call(
                    probe_id, role, f"logs/describe/{label}", logs.describe_log_streams,
                    {"logGroupName": config[label], "limit": 1}, expected,
                    {"AccessDenied", "AccessDeniedException"},
                ))
    passed = all(results)
    emit(probe_id, role, "complete", passed=passed, access_checks=len(results), failed_checks=results.count(False))
    return 0 if passed else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--role", required=True)
    args = parser.parse_args()
    probe_id = "unknown"
    try:
        with open(args.config, encoding="utf-8") as handle:
            config = json.load(handle)
        probe_id = config["probe_id"]
        return run(config, args.role)
    except Exception as error:
        # Even unexpected SDK/credential errors must not leak request data or values.
        emit(probe_id, args.role, "failed", passed=False, error_type=type(error).__name__)
        return 1


if __name__ == "__main__":
    sys.exit(main())
