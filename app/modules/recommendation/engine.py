"""Candidate search and comparison recovered from the unmerged structured implementation."""

import asyncio
from datetime import UTC, datetime

from app.core.exceptions import ServiceError
from app.modules.ai.explanation import explain
from app.modules.recommendation.cards import card, result, unavailable_parking
from app.modules.recommendation.ranking import DEFAULT_POLICY, rank_candidates
from app.schemas.recommendations import ConditionUpdate, RecommendationResponse, Stages

PURPOSE_QUERIES = {"food": "음식점", "cafe": "카페", "walk": "공원", "exhibition": "전시관"}
CATEGORY_FILTERS = {"food": "FD6", "cafe": "CE7"}


class DestinationRecommendationEngine:
    def __init__(self, source, model=None, policy=DEFAULT_POLICY):
        self.source, self.model, self.policy = source, model, policy

    async def recommend(self, request, conditions):
        async with asyncio.timeout(90):
            return await self._recommend(request, conditions)

    async def _recommend(self, request, conditions):
        origin = conditions.origin.coordinates
        excluded, seen = set(request.excluded_place_ids), set()
        eligible, failed, warnings = [], [], []
        route_successes = 0
        pages = {item.purpose: 1 for item in conditions.purposes}
        ended = set()
        budget = self.policy.max_pages_per_request
        semaphore = asyncio.Semaphore(5)

        async def route(candidate):
            async with semaphore:
                try:
                    return await self.source.route(origin, candidate.place.coordinates)
                except ServiceError as exc:
                    return exc

        while len(eligible) < 3 and budget and len(ended) < len(pages):
            for purpose in conditions.purposes:
                if purpose.purpose in ended or not budget:
                    continue
                query = " ".join(filter(None, (purpose.keyword, PURPOSE_QUERIES[purpose.purpose])))
                candidates, is_end = await self.source.search(
                    query, origin, purpose.purpose, pages[purpose.purpose]
                )
                pages[purpose.purpose] += 1
                budget -= 1
                if is_end or pages[purpose.purpose] > 45:
                    ended.add(purpose.purpose)
                usable = []
                for candidate in candidates:
                    place = candidate.place
                    if place.place_id in excluded or place.place_id in seen:
                        continue
                    expected_category = CATEGORY_FILTERS.get(purpose.purpose)
                    if expected_category and place.category_code != expected_category:
                        continue
                    seen.add(place.place_id)
                    if await self.source.region(place.coordinates) != "seoul":
                        continue
                    usable.append(candidate)
                answers = await asyncio.gather(*(route(item) for item in usable))
                for candidate, answer in zip(usable, answers, strict=True):
                    if isinstance(answer, ServiceError):
                        if answer.code in {"RATE_LIMITED", "SERVICE_UNAVAILABLE"}:
                            raise answer
                        failed.append(candidate)
                        continue
                    seconds, meters = answer
                    route_successes += 1
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
        routes = (
            "partial"
            if failed and route_successes
            else "failed"
            if failed
            else "ok"
            if route_successes
            else "skipped"
        )
        if failed:
            warnings.append(
                {
                    "code": "ROUTE_PARTIAL_FAILURE" if route_successes else "ROUTE_ALL_FAILED",
                    "message": "일부 경로를 확인하지 못했습니다."
                    if route_successes
                    else "모든 경로를 확인하지 못했습니다.",
                }
            )
        if len(ended) < len(pages) and len(eligible) < 3:
            warnings.append(
                {
                    "code": "SEARCH_INCOMPLETE",
                    "message": "이번 탐색 범위 밖에 추가 후보가 있을 수 있습니다.",
                }
            )
        selected = rank_candidates(eligible)[:3]
        kind, payload, explanation = "no_candidates", None, "skipped"
        if selected:
            descriptions, source = await explain(self.model, selected)
            payload = result(
                conditions,
                [
                    card(candidate, rank, description[0], source)
                    for rank, (candidate, description) in enumerate(
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
        elif failed and not route_successes:
            kind, payload = (
                "general_candidates",
                result(conditions, [card(candidate) for candidate in failed[:3]]),
            )
        before, after = (
            request.current_conditions.model_dump(mode="json"),
            conditions.model_dump(mode="json"),
        )
        changes = [
            {"field": field, "before": before[field], "after": value}
            for field, value in after.items()
            if value != before[field]
        ]
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
            warnings=warnings,
        )
