"""Load settings once, then replace this process with the requested command."""

import json
import os
import sys
from collections.abc import Sequence

from botocore.exceptions import BotoCoreError, ClientError
from pydantic import ValidationError
from pydantic_settings import SettingsError

from app.core.config import Settings
from app.core.secrets import StartupError, prepare_environment


def validate_command_settings(settings: Settings, command: Sequence[str]) -> None:
    if settings.runtime_role == "api" and settings.ai_provider == "openai":
        if not settings.openai_api_key.get_secret_value().strip():
            raise StartupError("secret_missing_openai_key")
    if settings.runtime_role == "import" and "jobs.import_topis_geometry" in command:
        if not settings.seoul_citydata_api_key.get_secret_value().strip():
            raise StartupError("secret_missing_citydata_key")


def main(argv: Sequence[str] | None = None) -> int:
    command = list(sys.argv[1:] if argv is None else argv)
    try:
        if not command:
            raise StartupError("command_required")
        environment = prepare_environment(os.environ)
        if environment.get("APP_RUNTIME_ENV") == "aws":
            # No child exists yet. All configuration errors terminate before exec.
            os.environ.clear()
            os.environ.update(environment)
            validate_command_settings(Settings(), command)
        os.execvpe(command[0], command, environment)
    except StartupError as exc:
        error_code = str(exc)
    except ClientError:
        error_code = "secret_access_failed"
    except BotoCoreError:
        error_code = "secret_connection_failed"
    except ValidationError, SettingsError:
        error_code = "invalid_application_settings"
    except OSError:
        error_code = "command_start_failed"
    except Exception:
        # Startup may handle plaintext secrets; never expose arbitrary exception details.
        error_code = "bootstrap_failed"
    print(json.dumps({"status": "failed", "error_code": error_code}), file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
