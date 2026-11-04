"""Scrub provider credentials before preserving evidence."""

import re

_SECRET_KEYS = {
    "key",
    "apikey",
    "servicekey",
    "accesstoken",
    "authorization",
    "consumersecret",
    "consumerkey",
    "password",
    "secret",
    "token",
    "refreshtoken",
}


def redact(value):
    """Remove credential fields and credential-bearing URL components recursively."""
    if isinstance(value, dict):
        return {
            key: redact(item)
            for key, item in value.items()
            if re.sub(r"[^a-z]", "", str(key).lower()) not in _SECRET_KEYS
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        value = re.sub(
            r"(?i)([?&](?:key|api_?key|serviceKey|accessToken|access_token)=)[^&#\s]*",
            r"\1[redacted]",
            value,
        )
        return re.sub(r"(openapi\.seoul\.go\.kr:8088/)[^/]+", r"\1[redacted]", value)
    return value
