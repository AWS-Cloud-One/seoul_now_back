"""Part 2 search progression, comparison and original-candidate route retry."""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime

from app.core.exceptions import ServiceError
from app.modules.ai.explanation import explain
from app.modules.recommendation.areas import select_areas
from app.modules.recommendation.cards import card, result, unavailable_parking
from app.modules.recommendation.progress import DEFAULT_PROGRESS_STORE, SearchProgress, fingerprint
from app.modules.recommendation.ranking import DEFAULT_POLICY, rank_candidates
from app.schemas.recommendation_result import SearchAreaSnapshot, SearchScope
from app.schemas.recommendations import ConditionUpdate, RecommendationResponse, Stages

PURPOSE_QUERIES = {"food": "음식점", "cafe": "카페", "walk": "공원", "exhibition": "전시관"}
CATEGORY_FILTERS = {"food": "FD6", "cafe": "CE7"}
FATAL_ERRORS = {"RATE_LIMITED", "SERVICE_UNAVAILABLE"}


class DestinationRecommendationEngine:
    def __init__(
        self, source, model=None, policy=DEFAULT_POLICY, *, progress_store=None, area_provider=None
    ):
        self.source, self.model, self.policy = source, model, policy
        self.progress_store = (
            progress_store if progress_store is not None else DEFAULT_PROGRESS_STORE
        )
        self.area_provider = area_provider

    async def recommend(self, request, conditions):
        async with asyncio.timeout(90):
            return await self._recommend(request, conditions)

    async def _recommend(self, request, conditions):
        trip = self.progress_store.trip(request.trip_id)
        signature = fingerprint(conditions.model_dump(mode="json"))
        progress = (
            deepcopy(trip.search)
            if trip.search.fingerprint == signature
            else SearchProgress(fingerprint=signature)
        )
        excluded = trip.excluded | set(request.excluded_place_ids)
        warnings = []
        if progress.areas is None:
            if self.area_provider is None:
                progress.areas = []
                progress.area_warnings = [
                    {
                        "code": "SEARCH_AREA_DATA_INCOMPLETE",
                        "message": "후보 동 자료 연결 전입니다. 출발지 주변만 탐색합니다.",
                    }
                ]
            else:
                try:
                    areas = await self.area_provider.areas(conditions.origin.coordinates)
                except ServiceError as exc:
                    if exc.code in FATAL_ERRORS:
                        raise
                    areas = []
                progress.areas, progress.area_warnings = select_areas(
                    areas, conditions.origin.coordinates, max_areas=self.policy.max_search_areas
                )
        warnings.extend(progress.area_warnings)
        areas = progress.areas or [None]
        targets = [(purpose, area) for area in areas for purpose in conditions.purposes]
        for purpose, area in targets:
            progress.pages.setdefault((purpose.purpose, area.area_id if area else "origin"), 1)

        pending, progress.remaining = progress.remaining, []
        eligible, failed, successes = await self._evaluate(pending, conditions, excluded, warnings)
        budget = self.policy.max_pages_per_request
        while len(eligible) < 3 and budget and len(progress.ended) < len(targets):
            for purpose, area in targets:
                key = (purpose.purpose, area.area_id if area else "origin")
                if key in progress.ended or not budget or len(eligible) >= 3:
                    continue
                query = " ".join(filter(None, (purpose.keyword, PURPOSE_QUERIES[purpose.purpose])))
                candidates, is_end = await self.source.search(
                    query,
                    area.center if area else conditions.origin.coordinates,
                    purpose.purpose,
                    progress.pages[key],
                )
                progress.pages[key] += 1
                budget -= 1
                if is_end or progress.pages[key] > 45:
                    progress.ended.add(key)
                    if not is_end:
                        warnings.append(
                            {
                                "code": "SEARCH_PROVIDER_LIMIT",
                                "message": "제공처 페이지 한도입니다. 서울 전체 소진은 아닙니다.",
                            }
                        )
                usable = []
                for candidate in candidates:
                    place = candidate.place
                    if place.place_id in excluded or place.place_id in progress.seen:
                        continue
                    expected = CATEGORY_FILTERS.get(purpose.purpose)
                    if expected and place.category_code != expected:
                        continue
                    if area and not area.contains(place.coordinates):
                        continue
                    if await self.source.region(place.coordinates) != "seoul":
                        continue
                    progress.seen.add(place.place_id)
                    usable.append(candidate)
                valid, invalid, count = await self._evaluate(usable, conditions, excluded, warnings)
                eligible.extend(valid)
                failed.extend(invalid)
                successes += count
        selected = rank_candidates(eligible, self.policy)[:3]
        selected_ids = {c.place.place_id for c in selected}
        progress.remaining = [
            c.model_copy(deep=True)
            for c in eligible + failed
            if c.place.place_id not in selected_ids
        ]
        incomplete = len(progress.ended) < len(targets) and len(selected) < 3
        if incomplete:
            warnings.append(
                {
                    "code": "SEARCH_INCOMPLETE",
                    "message": "탐색 예산에 도달했습니다. 추가 추천으로 이어서 탐색합니다.",
                }
            )
        response = await self._response(request, conditions, selected, failed, successes, warnings)
        if response.result is not None:
            response.result["search_scope"] = SearchScope(
                areas=[
                    SearchAreaSnapshot.model_validate(a.model_dump(exclude={"rings"}))
                    for a in progress.areas
                ],
                fallback=not bool(progress.areas),
                more_available=bool(progress.remaining) or len(progress.ended) < len(targets),
            ).model_dump(mode="json")
        trip.search = progress
        return response

    async def retry_routes(self, request, conditions, candidates):
        """Only Part 3's authenticated original candidates; no search or interpretation."""
        if not 1 <= len(candidates) <= 3:
            raise ServiceError("VALIDATION_ERROR", "재시도할 원래 후보는 1~3곳이어야 합니다.")
        trip = self.progress_store.trip(request.trip_id)
        excluded = trip.excluded | set(request.excluded_place_ids)
        warnings = []
        async with asyncio.timeout(90):
            eligible, failed, successes = await self._evaluate(
                candidates, conditions, excluded, warnings
            )
            return await self._response(
                request,
                conditions,
                rank_candidates(eligible, self.policy)[:3],
                failed,
                successes,
                warnings,
            )

    async def _evaluate(self, candidates, conditions, excluded, warnings):
        semaphore = asyncio.Semaphore(5)
        unique = {}
        for item in candidates:
            if item.place.place_id not in excluded:
                unique.setdefault(item.place.place_id, item.model_copy(deep=True))

        async def route(candidate):
            async with semaphore:
                try:
                    return await self.source.route(
                        conditions.origin.coordinates, candidate.place.coordinates
                    )
                except ServiceError as exc:
                    return exc

        eligible, failed, successes = [], [], 0
        values = list(unique.values())
        answers = await asyncio.gather(*(route(c) for c in values))
        for candidate, answer in zip(values, answers, strict=True):
            # Never retain an old ETA when its recheck failed.
            candidate.duration_seconds = candidate.distance_meters = (
                candidate.route_collected_at
            ) = None
            candidate.parking = None
            if isinstance(answer, ServiceError):
                if answer.code in FATAL_ERRORS:
                    raise answer
                failed.append(candidate)
                continue
            seconds, meters = answer
            successes += 1
            if (
                conditions.max_travel_seconds is not None
                and seconds > conditions.max_travel_seconds
            ):
                continue
            candidate.duration_seconds, candidate.distance_meters = seconds, meters
            candidate.route_collected_at = datetime.now(UTC)
            try:
                candidate.parking = await self.source.parking(candidate.place.coordinates)
            except ServiceError:
                candidate.parking = unavailable_parking("DATA_UNAVAILABLE", error=True)
                warnings.append(
                    {
                        "code": "PARKING_UNAVAILABLE",
                        "message": "일부 장소의 등록 주차정보를 확인하지 못했습니다.",
                    }
                )
            eligible.append(candidate)
        return eligible, failed, successes

    async def _response(self, request, conditions, selected, failed, successes, warnings):
        routes = (
            "partial"
            if failed and successes
            else "failed"
            if failed
            else "ok"
            if successes
            else "skipped"
        )
        if failed:
            warnings.append(
                {
                    "code": "ROUTE_PARTIAL_FAILURE" if successes else "ROUTE_ALL_FAILED",
                    "message": "일부 경로를 확인하지 못했습니다."
                    if successes
                    else "모든 경로를 확인하지 못했습니다.",
                }
            )
        kind, payload, explanation = "no_candidates", None, "skipped"
        if selected:
            descriptions, source = await explain(self.model, selected)
            payload = result(
                conditions,
                [
                    card(c, rank, description[0], source)
                    for rank, (c, description) in enumerate(
                        zip(selected, descriptions, strict=True), 1
                    )
                ],
            )
            kind, explanation = "recommendation", "ok" if source == "ai" else "fallback"
            if source != "ai":
                warnings.append(
                    {
                        "code": "EXPLANATION_FALLBACK",
                        "message": "확인된 근거로 작성한 기본 설명을 제공합니다.",
                    }
                )
        elif failed and not successes:
            kind, payload = "general_candidates", result(conditions, [card(c) for c in failed[:3]])
        before, after = (
            request.current_conditions.model_dump(mode="json"),
            conditions.model_dump(mode="json"),
        )
        changes = [
            {"field": field, "before": before[field], "after": value}
            for field, value in after.items()
            if value != before[field]
        ]
        if payload is not None:
            payload["ranking_policy"] = self.policy.snapshot().model_dump(mode="json")
        return RecommendationResponse(
            trip_id=request.trip_id,
            base_condition_version=request.condition_version,
            kind=kind,
            message={
                "recommendation": "조건에 맞는 장소를 추천합니다.",
                "general_candidates": "이동시간을 확인하지 못한 일반 후보입니다.",
                "no_candidates": "확인한 범위에서 조건에 맞는 후보가 없습니다.",
            }[kind],
            condition_update=ConditionUpdate(
                applied=True,
                conditions=conditions,
                version=request.condition_version + bool(changes),
                changes=changes,
            ),
            stages=Stages(routes=routes, explanation=explanation),
            result=payload,
            clarification=None,
            warnings=list({w["code"]: w for w in warnings}.values()),
        )
