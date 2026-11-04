"""Shared source/target identities, processor version and deterministic fingerprints."""

import hashlib
import json

PROCESSOR_VERSION = "normalized-2026-11-04.17"


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()


def source_target(key):
    if key.startswith("topis_attempt:"):
        return "topis_traffic", "link:" + key.split(":", 1)[1], "attempt"
    if key.endswith(":attempt"):
        source, target, _ = source_target(key.removesuffix(":attempt"))
        return source, target, "attempt"
    if key.startswith("topis:"):
        return "topis_traffic", "link:" + key.split(":", 1)[1], "data"
    if key.startswith("topis_geometry:"):
        return "topis_geometry", "file:topis_vertices", "data"
    if key.endswith("_status"):
        return (
            "topis_traffic" if key.startswith("topis") else key.removesuffix("_status"),
            "report:" + key,
            "report",
        )
    return key, "all", "data"
