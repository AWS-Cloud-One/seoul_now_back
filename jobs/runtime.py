"""Shared job paths, JSON reporting and process exit handling."""

import asyncio
import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REFERENCE_DATA = PROJECT_ROOT / "data/reference"
RAW_DATA = PROJECT_ROOT / "data/raw"
EXIT_CODES = {"success": 0, "failed": 1, "partial": 2, "skipped": 3, "paused_provider_limit": 4}


def report(result: dict) -> None:
    print(json.dumps(result, ensure_ascii=False), flush=True)


def finish(result: dict) -> int:
    report(result)
    return EXIT_CODES.get(result["status"], 1)


def run_cli(operation, *args) -> None:
    try:
        code = asyncio.run(operation(*args))
    except KeyboardInterrupt:
        code = 130
    except Exception:
        # Arbitrary exception text can contain credentials or provider URLs.
        code = finish({"status": "failed", "error_code": "configuration_or_database_error"})
    raise SystemExit(code)
