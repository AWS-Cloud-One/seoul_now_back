"""Join service-link geometry to its own TOPIS observation, never by similar IDs."""

import math
from datetime import UTC, datetime, timedelta


def network_feature(feature, observation=None, attempt=None, now=None):
    current = now or datetime.now(UTC)
    result = {
        "type": "Feature",
        "id": feature["id"],
        "geometry": feature["geometry"],
        "properties": dict(feature["properties"]),
    }
    props = result["properties"]
    props.update(
        speed_kmh=None,
        travel_time_seconds=None,
        observation_status="not_collected",
        measurement_scope="TOPIS service link",
        provider_observed_at=None,
        traffic_collected_at=None,
    )
    if observation is None:
        if attempt and attempt.data.get("status") == "failed":
            props["last_attempt_at"] = attempt.collected_at.isoformat()
            props["observation_status"] = (
                "no_observation"
                if attempt.data.get("error_code") == "provider_no_observation"
                else "latest_attempt_failed"
            )
        return result
    props["traffic_collected_at"] = observation.collected_at.isoformat()
    if (
        attempt
        and attempt.collected_at > observation.collected_at
        and attempt.data.get("status") != "success"
    ):
        props["observation_status"] = "latest_attempt_failed"
        return result
    age = current - observation.collected_at
    if age < timedelta(0) or age > timedelta(minutes=15):
        props["observation_status"] = "stale"
        return result
    rows = observation.data.get("rows") or []
    matching = [row for row in rows if str(row.get("link_id")) == feature["id"]]
    if len(matching) != 1:
        props["observation_status"] = "invalid_observation"
        return result
    try:
        speed = float(matching[0]["prcs_spd"])
        travel = float(matching[0]["prcs_trv_time"])
        if not all(math.isfinite(v) and v >= 0 for v in (speed, travel)):
            raise ValueError
    except ValueError, TypeError, KeyError:
        props["observation_status"] = "invalid_observation"
        return result
    props.update(
        speed_kmh=speed, travel_time_seconds=travel, observation_status="recently_collected"
    )
    return result
