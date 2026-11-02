"""Archive successful collections in the transaction that updates latest records."""

import re
from uuid import uuid4

from sqlalchemy import insert

from app.models.external_api import ExternalApiObservation, ExternalApiResponse

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


def parking_observation(item):
    # normalize_parking owns this canonical EWKT representation.
    longitude, latitude = map(
        float, item["location"].removeprefix("SRID=4326;POINT(").removesuffix(")").split()
    )
    return {
        "record_key": item["id"],
        "name": item["name"][:300],
        "observed_at": item["source_updated_at"],
        "latitude": latitude,
        "longitude": longitude,
        "data": {
            "source_id": item["source_id"],
            "address": item["address"],
            "details": item["details"],
        },
    }


async def archive_response(session, run_id, source, operation, payload, collected_at, rows):
    response_id = uuid4()
    await session.execute(
        insert(ExternalApiResponse).values(
            id=response_id,
            run_id=run_id,
            source=source,
            operation=operation,
            payload=redact(payload),
            collected_at=collected_at,
        )
    )
    observations = [
        {"id": uuid4(), "response_id": response_id, **row, "data": redact(row["data"])}
        for row in rows
    ]
    for offset in range(0, len(observations), 500):
        await session.execute(insert(ExternalApiObservation), observations[offset : offset + 500])
