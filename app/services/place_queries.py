"""Independent place and route facts. No condition merging or recommendation."""

import math
from datetime import UTC, datetime

from pydantic import ValidationError

from app.clients.kakao import DRIVING_OPTIONS, KakaoError
from app.core.exceptions import ServiceError
from app.schemas.location import Coordinates
from app.schemas.map import MultiLineString
from app.schemas.query_services import DrivingData, DrivingRoute, FoundPlace, PlaceSearchData
from app.services.locations import documents, provider_error, provider_failure, text
from app.services.query_cursors import QUERY_CURSORS


def direct_distance(origin, destination):
    lat1, lat2 = math.radians(origin.latitude), math.radians(destination.latitude)
    delta_lat = lat2 - lat1
    delta_lon = math.radians(destination.longitude - origin.longitude)
    hav = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
    )
    return 6371008.8 * 2 * math.asin(min(1, math.sqrt(hav)))


def nonnegative(value, *, integer=False):
    valid = type(value) is int if integer else type(value) in (int, float)
    if not valid or not math.isfinite(value) or value < 0:
        raise ValueError("Invalid measurement")
    return value


class PlaceQueryService:
    def __init__(self, provider, *, cursors=None, driving_requests=None, cache_namespace=None):
        self.provider = provider
        self.cursors = cursors if cursors is not None else QUERY_CURSORS
        self.driving_requests, self.cache_namespace = driving_requests, cache_namespace

    def route_key(self, origin, destination, include_geometry):
        return (
            self.cache_namespace,
            "v1/directions",
            tuple(sorted(DRIVING_OPTIONS.items())),
            origin.longitude,
            origin.latitude,
            destination.longitude,
            destination.latitude,
            include_geometry,
        )

    def remember_route(self, origin, destination, route):
        if self.driving_requests is None or route.value is None or route.collected_at is None:
            return
        value = DrivingData(
            origin=origin,
            destination=destination,
            checked_at=route.collected_at,
            route=route.model_dump()
            | {
                "value": DrivingRoute(
                    **route.value.model_dump(),
                    geometry=None,
                    geometry_reason="NOT_REQUESTED",
                    taxi_fare_krw=None,
                    toll_fare_krw=None,
                )
            },
        )
        self.driving_requests.remember(self.route_key(origin, destination, False), value)

    async def search(
        self, query, limit=10, cursor=None, *, center=None, radius=None, category=None
    ):
        query = query.strip()
        if not query:
            raise ServiceError("VALIDATION_ERROR", "검색어를 입력해 주세요.")
        scope = ("places", query, limit, center.model_dump() if center else None, radius, category)
        state = (
            self.cursors.read(cursor, scope)
            if cursor
            else {"page": 1, "buffer": [], "ended": False}
        )
        items = state["buffer"]
        try:
            # A bounded amount of I/O; an underfilled page may still have a cursor.
            for _ in range(3):
                if len(items) >= limit or state["ended"]:
                    break
                if state["page"] > 45:
                    raise ServiceError(
                        "PROVIDER_ERROR", "제공처 검색 상한입니다. 검색 범위를 줄여 주세요."
                    )
                rows, ended = documents(
                    await self.provider.places(
                        query,
                        state["page"],
                        center=center,
                        radius=radius,
                        category=category,
                    )
                )
                now = datetime.now(UTC)
                for row in rows:
                    key, name = text(row.get("id")), text(row.get("place_name"))
                    if not key or not name:
                        raise provider_error()
                    point = Coordinates(latitude=row["y"], longitude=row["x"])
                    if category and row.get("category_group_code") != category:
                        continue
                    meters = direct_distance(center, point) if center else None
                    if meters is not None and meters > radius:
                        continue
                    items.append(
                        FoundPlace(
                            place_id="kakao:" + key,
                            name=name,
                            category_code=text(row.get("category_group_code")),
                            category_name=text(row.get("category_name")),
                            coordinates=point,
                            address=text(row.get("address_name")),
                            road_address=text(row.get("road_address_name")),
                            phone=text(row.get("phone")),
                            provider_url=text(row.get("place_url")),
                            distance_meters=meters,
                            collected_at=now,
                        )
                    )
                state["page"] += 1
                state["ended"] = ended
            state["buffer"] = items[limit:]
            more = bool(state["buffer"]) or not state["ended"]
            return PlaceSearchData(
                items=items[:limit],
                has_more=more,
                next_cursor=self.cursors.issue(scope, state) if more else None,
            )
        except KakaoError as exc:
            raise provider_failure(exc) from None
        except ValidationError, KeyError, TypeError, ValueError:
            raise provider_error() from None

    async def driving(self, origin, destination, *, include_geometry=True):
        if self.driving_requests is None:
            return await self._driving(origin, destination, include_geometry=include_geometry)
        value = await self.driving_requests.get(
            self.route_key(origin, destination, include_geometry),
            lambda: self._driving(origin, destination, include_geometry=include_geometry),
            aliases=(self.route_key(origin, destination, True),) if not include_geometry else (),
        )
        if not include_geometry:
            value.route.value.geometry = None
            value.route.value.geometry_reason = "NOT_REQUESTED"
        return value

    async def _driving(self, origin, destination, *, include_geometry):
        try:
            body = await self.provider.driving(
                origin, destination, include_geometry=include_geometry
            )
            if not isinstance(body, dict):
                raise ValueError("Invalid route response")
            routes = body.get("routes")
            if not isinstance(routes, list) or not routes or not isinstance(routes[0], dict):
                raise ValueError("Invalid routes")
            route = routes[0]
            if type(route.get("result_code")) is not int:
                raise ValueError("Invalid route result code")
            if route["result_code"] != 0:
                if route["result_code"] not in {1, 101, 102, 103, 104, 105, 106, 107}:
                    raise ValueError("Unknown route result code")
                raise ServiceError("NOT_FOUND", "자동차 경로를 찾을 수 없습니다.", retryable=False)
            summary = route["summary"]
            seconds = nonnegative(summary["duration"], integer=True)
            meters = nonnegative(summary["distance"])
            fare = summary.get("fare") or {}
            if not isinstance(fare, dict):
                fare = {}
            amounts = {}
            for key in ("taxi", "toll"):
                try:
                    amounts[key] = nonnegative(fare.get(key), integer=True)
                except ValueError:
                    amounts[key] = None
            geometry, reason = None, "NOT_REQUESTED"
            if include_geometry:
                try:
                    lines = []
                    for section in route["sections"]:
                        for road in section["roads"]:
                            vertices = road["vertexes"]
                            if len(vertices) < 4 or len(vertices) % 2:
                                raise ValueError("Invalid route vertices")
                            if any(
                                type(v) not in (int, float) or not math.isfinite(v)
                                for v in vertices
                            ):
                                raise ValueError("Invalid route coordinate")
                            lines.append(list(zip(vertices[::2], vertices[1::2], strict=True)))
                    geometry = MultiLineString(type="MultiLineString", coordinates=lines)
                    reason = None
                except ValueError, ValidationError, KeyError, TypeError:
                    reason = "PROVIDER_GEOMETRY_UNAVAILABLE"
            now = datetime.now(UTC)
            return DrivingData(
                origin=origin,
                destination=destination,
                checked_at=now,
                route={
                    "value": DrivingRoute(
                        duration_seconds=seconds,
                        distance_meters=meters,
                        geometry=geometry,
                        geometry_reason=reason,
                        taxi_fare_krw=amounts["taxi"],
                        toll_fare_krw=amounts["toll"],
                    ),
                    "status": "available",
                    "freshness": "fresh",
                    "observed_at": None,
                    "collected_at": now,
                    "source": "kakao_mobility",
                    "reason_code": None,
                },
            )
        except KakaoError as exc:
            raise provider_failure(exc) from None
        except ValidationError, KeyError, TypeError, ValueError:
            raise ServiceError("PROVIDER_ERROR", "자동차 경로 자료를 확인할 수 없습니다.") from None
