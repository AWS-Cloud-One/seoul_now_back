"""Shared processor version and deterministic fingerprints."""

import hashlib
import json

PROCESSOR_VERSION = "direct-2026-11-10.1"


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()
