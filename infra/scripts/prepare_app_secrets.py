"""Prepare private local PutSecretValue request files; make no AWS or DB calls.

Only the operator runs this against the real .env. stdout contains the temporary
directory path, never credentials. Upload commands are in the W4-A handoff.
"""

import argparse
import json
import os
import re
import secrets
import shutil
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

from dotenv import dotenv_values
from sqlalchemy.engine import URL

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from app.core.secrets import DB_KEY, ROLE_KEYS, parse_secret  # noqa: E402

ROLES = ("migration", "import", "collector", "api")
# OpenAI is optional under Bedrock; municipal parking can use the Seoul key.
OPTIONAL_KEYS = {"APP_OPENAI_API_KEY", "APP_SEOUL_MUNICIPAL_PARKING_API_KEY"}


class PreparationError(Exception):
    """Errors contain key names or fixed messages, never key values."""


def prepare(config, mapping, values, parent=None):
    payloads = {}
    for role in ROLES:
        arn = mapping["secrets"][role]["arn"]
        pattern = (
            rf"arn:aws:secretsmanager:{re.escape(config['region'])}:"
            rf"{re.escape(config['account'])}:secret:seoul-now/lab/{role}-[A-Za-z0-9]{{6}}"
        )
        if not re.fullmatch(pattern, arn):
            raise PreparationError("secret_target_does_not_match_config")
        keys = ROLE_KEYS[role] - {DB_KEY}
        missing = sorted(key for key in keys - OPTIONAL_KEYS if not values.get(key, "").strip())
        if missing:
            raise PreparationError("missing_env_keys: " + ", ".join(missing))
        payload = {key: values[key] for key in sorted(keys) if values.get(key, "").strip()}
        payload[DB_KEY] = URL.create(
            "postgresql+psycopg",
            username=f"seoul_now_{role}",
            password=secrets.token_urlsafe(32),
            host=config["db_host"],
            port=config["db_port"],
            database=config["db_name"],
        ).render_as_string(hide_password=False)
        text = json.dumps(payload, ensure_ascii=False)
        parse_secret(text, role)
        payloads[role] = {
            "SecretId": arn,
            # Retrying the SAME file cannot silently generate a different password/version.
            "ClientRequestToken": str(uuid4()),
            "SecretString": text,
        }
    directory = Path(tempfile.mkdtemp(prefix="seoul-now-app-secrets-", dir=parent))
    try:
        for role, request in payloads.items():
            path = directory / f"{role}.request.json"
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as target:
                json.dump(request, target, ensure_ascii=False, indent=2)
                target.write("\n")
    except BaseException:
        shutil.rmtree(directory)
        raise
    return directory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument(
        "--config", type=Path, default=ROOT / "agent/records/w4-db-bootstrap.config.json"
    )
    parser.add_argument(
        "--mapping", type=Path, default=ROOT / "agent/records/w3-pod-access.config.json"
    )
    args = parser.parse_args()
    try:
        if not args.env_file.is_file():
            raise PreparationError("env_file_not_found")
        # Do not substitute values from the shell environment into credentials.
        values = {
            key: value or ""
            for key, value in dotenv_values(args.env_file, interpolate=False).items()
        }
        directory = prepare(
            json.loads(args.config.read_text()), json.loads(args.mapping.read_text()), values
        )
        print(directory)
        return 0
    except Exception as error:
        message = str(error) if isinstance(error, PreparationError) else type(error).__name__
        print("Secret file preparation failed: " + message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
