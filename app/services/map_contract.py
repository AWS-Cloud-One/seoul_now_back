"""Freshness policy for published observations; no external requests."""

import json
import math
from datetime import UTC, datetime, timedelta

from pydantic import TypeAdapter, ValidationError

from app.api.contract import ContractError
from app.repositories.map import MapRepository
from app.schemas.map import GeometryData, MapQuery, StatusData

GEOMETRY_DATA = TypeAdapter(GeometryData)
STATUS_DATA = TypeAdapter(StatusData)


def parse_bbox(raw: str) -> tuple[float, float, float, float]:
    try:
        return MapQuery(bbox=tuple(float(v) for v in raw.split(",")), level=1).bbox
    except ValueError, ValidationError:
        raise ContractError(
            422, "INVALID_MAP_QUERY", "bbox의 경위도 범위를 확인해 주세요."
        ) from None


class MapService:
    def __init__(self, repository: MapRepository, max_features: int = 10000):
        # Existing operational guardrail; final level-specific limits remain TBD.
        self.repository, self.max_features = repository, max_features

    async def selection(self, layer, query, version=None, *, include_geometry=False):
        published = await self.repository.get_version(layer)
        if published is None or not published.is_ready or not published.geometry_version:
            raise ContractError(503, "DATA_UNAVAILABLE", "지도 기준 자료가 준비되지 않았습니다.")
        if version is not None and version != published.geometry_version:
            raise ContractError(
                409, "GEOMETRY_VERSION_MISMATCH", "지도 위치 자료를 다시 조회해 주세요."
            )
        features = await self.repository.select_features(
            layer,
            query.bbox,
            self.max_features,
            include_geometry=include_geometry,
        )
        if len(features) > self.max_features:
            raise ContractError(422, "MAP_RANGE_TOO_LARGE", "지도 조회 범위를 줄여 주세요.")
        return published, features

    async def geometry(self, layer, query: MapQuery):
        published, features = await self.selection(layer, query, include_geometry=True)
        try:
            return GEOMETRY_DATA.validate_python(
                {
                    **query.model_dump(),
                    "layer": layer,
                    "geometry_version": published.geometry_version,
                    "coverage": published.coverage,
                    "feature_collection": {
                        "type": "FeatureCollection",
                        "features": [
                            {
                                "type": "Feature",
                                "id": f.feature_id,
                                "properties": f.properties,
                                "geometry": json.loads(f.geometry),
                            }
                            for f in features
                        ],
                    },
                }
            )
        except ValueError, TypeError, ValidationError:
            raise ContractError(
                503, "DATA_UNAVAILABLE", "지도 기준 자료를 확인할 수 없습니다."
            ) from None

    async def status(self, layer, query: MapQuery, version):
        published, features = await self.selection(layer, query, version)
        ids = [f.feature_id for f in features]
        rows = await self.repository.get_observations(layer, ids)
        now = datetime.now(UTC)
        field = "traffic" if layer == "roads" else "population"
        items = [
            {
                "link_id" if layer == "roads" else "area_code": key,
                field: information(layer, rows.get(key), now),
            }
            for key in ids
        ]
        partial = any(
            item[field]["status"] != "available"
            or item[field]["reason_code"] == "COLLECTION_FAILED"
            or any(
                v is None for k, v in (item[field]["value"] or {}).items() if k != "is_replacement"
            )
            for item in items
        )
        try:
            return STATUS_DATA.validate_python(
                {
                    **query.model_dump(),
                    "layer": layer,
                    "geometry_version": published.geometry_version,
                    "status": "partial" if partial else "ok",
                    "items": items,
                }
            )
        except ValidationError:
            raise ContractError(
                503, "DATA_UNAVAILABLE", "지도 상태 자료를 확인할 수 없습니다."
            ) from None


def valid_time(value, now):
    if isinstance(value, datetime) and value.tzinfo is not None and value <= now:
        return value
    return None


def normalized_value(layer, value):
    """Invalid individual measurements remain null, never zero or prior values."""
    if not isinstance(value, dict):
        return None

    def numeric(key, integer=False):
        raw = value.get(key)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return None
        if not math.isfinite(raw) or raw < 0 or (integer and not float(raw).is_integer()):
            return None
        return int(raw) if integer else raw

    if layer == "roads":
        return {"speed_kmh": numeric("speed_kmh"), "travel_seconds": numeric("travel_seconds")}
    low, high = numeric("min_people", True), numeric("max_people", True)
    if low is not None and high is not None and low > high:
        low = high = None
    congestion = value.get("congestion")
    return {
        "min_people": low,
        "max_people": high,
        "congestion": congestion
        if congestion
        in (
            "relaxed",
            "normal",
            "slightly_crowded",
            "crowded",
        )
        else None,
        "is_replacement": value.get("is_replacement")
        if isinstance(value.get("is_replacement"), bool)
        else None,
    }


def information(layer, row, now):
    observed = valid_time(row.observed_at, now) if row else None
    collected = valid_time(row.collected_at, now) if row else None
    failed = row.collection_failed if row else False
    value = normalized_value(layer, row.value) if row else None
    result = {
        "value": None,
        "status": "unavailable",
        "freshness": "unknown",
        "observed_at": observed,
        "collected_at": collected,
        "source": "topis_traffic" if layer == "roads" else "seoul_city_population",
        "reason_code": "NO_RECORD",
    }
    if value is None:
        if failed:
            result.update(status="error", reason_code="COLLECTION_FAILED")
        return result
    stamp = collected if layer == "roads" else observed
    if stamp is None or stamp.tzinfo is None or stamp > now:
        result["reason_code"] = "SOURCE_TIME_UNKNOWN"
        return result
    age = now - stamp
    if layer == "roads":
        fields = ("speed_kmh", "travel_seconds")
        if age > timedelta(minutes=30):
            result["reason_code"] = "STALE_DATA"
            return result
        if observed is not None and observed.tzinfo is not None and observed <= now:
            result["freshness"] = "fresh" if now - observed <= timedelta(minutes=30) else "stale"
    else:
        fields = ("min_people", "max_people", "congestion")
        result["freshness"] = "fresh" if age <= timedelta(minutes=60) else "stale"
        if age > timedelta(minutes=120):
            result["reason_code"] = "STALE_DATA"
            return result
        if value.get("is_replacement") is True:
            result["reason_code"] = "REPLACEMENT_VALUE"
            return result
    if all(value.get(field) is None for field in fields):
        return result
    keys = fields if layer == "roads" else (*fields, "is_replacement")
    result.update(
        value={key: value.get(key) for key in keys},
        status="available",
        reason_code="COLLECTION_FAILED"
        if failed
        else (
            "SOURCE_TIME_UNKNOWN"
            if result["freshness"] == "unknown"
            else ("STALE_DATA" if result["freshness"] == "stale" else None)
        ),
    )
    return result
