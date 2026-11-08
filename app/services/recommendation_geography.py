"""Bounded source-grade reads; never infer congestion from speed or parking."""

import asyncio
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.exc import SQLAlchemyError

from app.core.exceptions import ServiceError

SOURCE = "seoul_citydata:road_idx"
GRADES = {"원활", "서행", "정체"}


@dataclass(frozen=True)
class RoadGrade:
    grade: str | None = None
    observed_at: datetime | None = None
    source: str | None = None
    reason: str | None = "NO_OBSERVATION"


def source_time(value):
    if not isinstance(value, str):
        return None
    try:
        at = datetime.fromisoformat(value.strip())
        # Seoul's source uses local wall time, never the application's receipt time.
        return (at if at.tzinfo else at.replace(tzinfo=ZoneInfo("Asia/Seoul"))).astimezone(UTC)
    except ValueError:
        return None


def parse_grades(sections):
    section = sections.get("ROAD_TRAFFIC_STTS")
    if not isinstance(section, dict):
        return {}
    average = section.get("AVG_ROAD_DATA")
    at = source_time(average.get("ROAD_TRAFFIC_TIME")) if isinstance(average, dict) else None
    rows = section.get("ROAD_TRAFFIC_STTS")
    if not isinstance(rows, list):
        return {}
    result = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        link_id = row.get("LINK_ID")
        if not isinstance(link_id, str) or not link_id.strip():
            continue
        grade = row.get("IDX")
        grade = grade.strip() if isinstance(grade, str) else None
        item = RoadGrade(
            grade if grade in GRADES else None,
            at,
            SOURCE,
            None if grade in GRADES else "NO_SOURCE_GRADE",
        )
        link_id = link_id.strip()
        if link_id in result and result[link_id] != item:
            item = RoadGrade(None, at, SOURCE, "CONFLICTING_ROAD_GRADES")
        result[link_id] = item
    return result


def consensus(link_ids, observations, *, now=None):
    if not link_ids or None in link_ids:
        return RoadGrade(reason="ROAD_MAPPING_UNAVAILABLE")
    values = [observations.get(link_id) for link_id in sorted(link_ids)]
    if any(item is None for item in values):
        return RoadGrade(reason="INCOMPLETE_ROAD_COVERAGE")
    now = now or datetime.now(UTC)
    for item in values:
        if item.observed_at is None:
            return RoadGrade(source=SOURCE, reason="NO_OBSERVATION_TIME")
        if item.observed_at > now:
            return RoadGrade(source=SOURCE, reason="INVALID_OBSERVATION_TIME")
        if now - item.observed_at > timedelta(minutes=30):
            return RoadGrade(None, item.observed_at, SOURCE, "STALE_DATA")
        if item.grade is None:
            return item
    at = min(item.observed_at for item in values)
    if len({item.grade for item in values}) != 1:
        return RoadGrade(None, at, SOURCE, "CONFLICTING_ROAD_GRADES")
    return RoadGrade(values[0].grade, at, SOURCE, None)


class RecommendationGeographyService:
    # Request-scoped limits, not a polling worker or the stage-4 ETA cache.
    MAX_HOTSPOTS = 6
    INITIAL_HOTSPOTS = 3
    NEARBY_ROAD_RADIUS_M = 100
    FETCH_BUDGET_SECONDS = 15

    def __init__(self, repository, provider):
        self.repository, self.provider = repository, provider
        self.observations = {}
        self.loaded = set()
        self.provider_error = None
        self.fetch_seconds = 0

    async def _load(self, coordinates, *, covering):
        if self.provider_error is not None:
            return
        remaining = self.FETCH_BUDGET_SECONDS - self.fetch_seconds
        if remaining <= 0:
            self.provider_error = "PROVIDER_TIMEOUT"
            return
        hotspots = await self.repository.hotspots(
            coordinates,
            covering=covering,
            limit=self.INITIAL_HOTSPOTS,
        )
        pending = []
        for code, name in hotspots:
            if code in self.loaded or len(self.loaded) >= self.MAX_HOTSPOTS:
                continue
            self.loaded.add(code)
            pending.append((code, name))

        async def fetch(code, name):
            try:
                return parse_grades(
                    await self.provider.fetch_sections(
                        code,
                        name,
                        ("ROAD_TRAFFIC_STTS",),
                    )
                )
            except ServiceError as exc:
                return exc

        started = time.monotonic()
        try:
            async with asyncio.timeout(remaining):
                answers = await asyncio.gather(*(fetch(code, name) for code, name in pending))
        except TimeoutError:
            self.provider_error = "PROVIDER_TIMEOUT"
            return
        finally:
            self.fetch_seconds += time.monotonic() - started
        for answer in answers:
            if isinstance(answer, ServiceError):
                # Do not continue spending calls after quota/configuration failure.
                if answer.code in {"RATE_LIMITED", "SERVICE_UNAVAILABLE"}:
                    self.provider_error = answer.code
                continue
            for link_id, item in answer.items():
                old = self.observations.get(link_id)
                if old is None:
                    self.observations[link_id] = item
                elif old.observed_at is None or item.observed_at is None:
                    self.observations[link_id] = RoadGrade(
                        source=SOURCE, reason="NO_OBSERVATION_TIME"
                    )
                elif item.observed_at > old.observed_at:
                    self.observations[link_id] = item
                elif item.observed_at == old.observed_at and item != old:
                    self.observations[link_id] = RoadGrade(
                        None,
                        item.observed_at,
                        SOURCE,
                        "CONFLICTING_ROAD_GRADES",
                    )

    async def areas(self, origin):
        try:
            areas = await self.repository.areas()
        except SQLAlchemyError:
            return []
        if not areas:
            return []
        try:
            parking = await self.repository.parking_counts()
        except SQLAlchemyError:
            parking = None
        if parking is not None:
            counts, checked_at = parking
            for area in areas:
                area.registered_parking_count = counts.get(area.area_id, 0)
                area.parking_checked_at = checked_at
        try:
            roads = await self.repository.area_roads()
            if roads:
                await self._load(origin, covering=False)
            for area in areas:
                value = consensus(roads.get(area.area_id), self.observations)
                area.congestion = value.grade or "확인 불가"
                area.road_observed_at = value.observed_at
        except SQLAlchemyError:
            pass
        return areas

    async def traffic(self, coordinates):
        try:
            roads = await self.repository.nearby_roads(coordinates, self.NEARBY_ROAD_RADIUS_M)
            if roads and None not in roads:
                await self._load(coordinates, covering=True)
            value = consensus(roads, self.observations)
            if value.reason == "INCOMPLETE_ROAD_COVERAGE" and self.provider_error:
                return RoadGrade(reason=self.provider_error)
            return value
        except SQLAlchemyError:
            return RoadGrade(reason="DATA_UNAVAILABLE")
