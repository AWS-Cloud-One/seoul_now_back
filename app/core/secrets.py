"""Role-scoped startup secrets. Never log payloads or SDK exception messages."""

import json
import re
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass

import boto3
from botocore.config import Config

DB_KEY = "APP_DATABASE_URL"
CITYDATA_KEY = "APP_SEOUL_CITYDATA_API_KEY"
ROLE_KEYS = {
    "api": frozenset({DB_KEY, CITYDATA_KEY, "APP_KAKAO_REST_API_KEY", "APP_OPENAI_API_KEY"}),
    "collector": frozenset(
        {
            DB_KEY,
            CITYDATA_KEY,
            "APP_SEOUL_API_KEY",
            "APP_SEOUL_TOPIS_API_KEY",
            "APP_SEOUL_MUNICIPAL_PARKING_API_KEY",
            "APP_SEOUL_SDOT_API_KEY",
            "APP_SGIS_CONSUMER_KEY",
            "APP_SGIS_CONSUMER_SECRET",
            "APP_DATA_GO_KR_API_KEY",
        }
    ),
    "migration": frozenset({DB_KEY}),
    "import": frozenset({DB_KEY, CITYDATA_KEY}),
}
# Include the legacy ITS setting when removing inherited sensitive configuration.
SENSITIVE_KEYS = frozenset().union(*ROLE_KEYS.values(), {"APP_ITS_API_KEY"})
SECRET_ARN = re.compile(
    r"arn:(?:aws|aws-us-gov|aws-cn):secretsmanager:([a-z0-9-]+):[0-9]{12}:"
    r"secret:[A-Za-z0-9/_+=.@-]+-[A-Za-z0-9]{6}"
)


class StartupError(Exception):
    """Contains only a fixed, safe error code."""


@dataclass(frozen=True)
class SecretRequest:
    role: str
    secret_id: str
    region: str

    @classmethod
    def from_environment(cls, environ: Mapping[str, str]):
        role = environ.get("APP_RUNTIME_ROLE", "")
        secret_id = environ.get("APP_SETTINGS_SECRET_ID", "")
        region = environ.get("AWS_REGION", "")
        match = SECRET_ARN.fullmatch(secret_id)
        if role not in ROLE_KEYS or not match or match[1] != region:
            raise StartupError("invalid_bootstrap_configuration")
        return cls(role, secret_id, region)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise StartupError("secret_duplicate_key")
        result[key] = value
    return result


def parse_secret(payload: str, role: str) -> dict[str, str]:
    try:
        values = json.loads(payload, object_pairs_hook=_unique_object)
    except ValueError, TypeError, RecursionError:
        raise StartupError("secret_invalid_json") from None
    if not isinstance(values, dict):
        raise StartupError("secret_invalid_object")
    if values.keys() - ROLE_KEYS[role]:
        raise StartupError("secret_unexpected_key")
    for value in values.values():
        if not isinstance(value, str) or "\x00" in value:
            raise StartupError("secret_invalid_value")
        try:
            value.encode("utf-8")
        except UnicodeError:
            raise StartupError("secret_invalid_value") from None
    required = {DB_KEY}
    if role == "api":
        required.update({CITYDATA_KEY, "APP_KAKAO_REST_API_KEY"})
    if any(not values.get(key, "").strip() for key in required):
        raise StartupError("secret_missing_required")
    return values


def load_secret(request: SecretRequest) -> dict[str, str]:
    # Pod Identity, aws login, or other standard SDK credentials; no keys in this payload.
    with closing(
        boto3.Session().client(
            "secretsmanager",
            region_name=request.region,
            config=Config(
                connect_timeout=3,
                read_timeout=5,
                retries={"total_max_attempts": 2, "mode": "adaptive"},
                ignore_configured_endpoint_urls=True,
            ),
        )
    ) as client:
        try:
            response = client.get_secret_value(
                SecretId=request.secret_id, VersionStage="AWSCURRENT"
            )
        except client.exceptions.ResourceNotFoundException:
            raise StartupError("secret_not_found") from None
        except client.exceptions.DecryptionFailure:
            raise StartupError("secret_decryption_failed") from None
        except client.exceptions.InvalidRequestException:
            raise StartupError("secret_unavailable") from None
    payload = response.get("SecretString")
    if not isinstance(payload, str):
        raise StartupError("secret_string_required")
    return parse_secret(payload, request.role)


def prepare_environment(environ: Mapping[str, str]) -> dict[str, str]:
    mode = environ.get("APP_RUNTIME_ENV", "local")
    if mode == "local":
        return dict(environ)
    if mode != "aws":
        raise StartupError("invalid_runtime_environment")
    request = SecretRequest.from_environment(environ)
    values = load_secret(request)
    # Absent role secrets must not fall back to stale or differently cased env keys.
    result = {key: value for key, value in environ.items() if key.upper() not in SENSITIVE_KEYS}
    result.update(values)
    result["APP_SETTINGS_LOADED"] = "1"
    return result
