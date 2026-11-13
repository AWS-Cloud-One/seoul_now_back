"""V2 destination-scoped recommendation with optional, genuine driving facts."""

import asyncio
from datetime import UTC, datetime

from app.core.exceptions import ServiceError
from app.modules.ai.explanation import explain
from app.modules.chat_v2.candidate_conditions import assess
from app.modules.chat_v2.contracts import RecommendationPlan
from app.modules.chat_v2.control import checkpoint
from app.modules.chat_v2.predicates import EvaluationSummary
from app.modules.chat_v2.preparation import fail_contract
from app.modules.chat_v2.region_contracts import RegionSearchScope
from app.modules.chat_v2.region_repository import bounding_rect, official_areas, seoul_shape
from app.modules.chat_v2.results import PreparedRecommendation, StageError
from app.modules.recommendation.areas import SearchArea
from app.modules.recommendation.cards import card, unavailable_parking
from app.modules.recommendation.engine import CATEGORY_FILTERS, PURPOSE_QUERIES
from app.modules.recommendation.ranking import DEFAULT_POLICY, rank_candidates
from app.repositories.recommendation_geography import RecommendationGeographyRepository
from app.schemas.recommendation_result import ContextCard, SearchAreaSnapshot, SearchScope
from app.services.place_queries import direct_distance


def regional_reason(candidate, label):
    """Use each candidate's verified facts, without inventing qualitative rankings."""
    place, parking = candidate.place, candidate.parking
    category = place.category_name.rsplit(" > ", 1)[-1] if place.category_name else "장소"
    address = place.road_address or place.address
    lines = [f"{place.name}: {label}에서 찾은 후보이며, 제공된 업종 분류는 ‘{category}’입니다."]
    if address:
        lines.append(f"등록 주소는 {address}입니다.")
    if parking and parking.status == "available":
        if parking.total_count == 0:
            lines.append(f"주변 {parking.search_radius_meters}m 안에 등록 주차장이 없어요.")
        elif parking.nearest_distance_meters is not None:
            distance = max(1, round(parking.nearest_distance_meters))
            lines.append(f"가장 가까운 등록 주차장까지 직선거리 약 {distance}m예요.")
    return " ".join(lines)


class DestinationRecommendationPreparer:
    def __init__(self, legacy, session, source, geography, *, identity, work):
        self.legacy, self.session, self.source, self.geography = legacy, session, source, geography
        self.identity, self.work = identity, work

    async def prepare(self, plan):
        if not isinstance(plan, RecommendationPlan):
            return await self.legacy.prepare(plan)
        plan = RecommendationPlan.model_validate(plan.model_dump())
        if (
            plan.context.identity != self.identity
            or plan.context.current_conditions != self.work.conditions
            or plan.context.condition_version != self.work.version
            or self.work.invalidated
        ):
            raise fail_contract()
        async with asyncio.timeout(90):
            return await self._prepare(plan)

    async def _prepare(self, plan):
        conditions = plan.effective_conditions
        mode = "driving" if conditions.origin else "regional"
        warnings, searched, candidates, seen = [], [], [], set(self.work.excluded)
        region = plan.search_area
        spatial = region is not None and region.kind != "administrative"
        seoul = await seoul_shape(self.session) if spatial else None
        if spatial and seoul is None:
            return self._missing(plan, mode)
        if region and region.kind == "official":
            areas = [
                a for a in await official_areas(self.session) if a.area_id in region.boundary_ids
            ]
            if {a.area_id: a.geometry_version for a in areas} != region.boundary_versions:
                return self._missing(plan, mode)
        elif region and region.kind == "radius":
            if not seoul.contains(region.anchor.coordinates):
                return self._missing(plan, mode)
            # A transient scan target, never persisted as an administrative SearchAreaSnapshot.
            areas = [
                SearchArea(
                    area_id=region.anchor.place_id,
                    name=region.anchor.name,
                    center=region.anchor.coordinates,
                    source="kakao_local",
                    geometry_version="radius-v1",
                )
            ]
        else:
            areas = await RecommendationGeographyRepository(self.session).areas()
            if region:
                wanted = set(region.area_ids)
                areas = [a for a in areas if a.area_id in wanted]
                if {a.area_id for a in areas} != wanted:
                    return self._missing(plan, mode)
        if not areas:
            return self._missing(plan, mode)
        # The origin does not determine the search region or its scan order.
        areas = sorted(areas, key=lambda a: a.area_id)
        targets = [(area, purpose) for area in areas for purpose in conditions.purposes]
        budget, more, failures = 3, False, 0
        for target_index, (area, purpose) in enumerate(targets):
            if not budget:
                more = True
                break
            searched.append(area)
            query = " ".join(
                filter(
                    None,
                    (
                        None if spatial else area.name,
                        purpose.keyword,
                        PURPOSE_QUERIES[purpose.purpose],
                    ),
                )
            )
            for page in range(1, 4):
                budget -= 1
                await checkpoint("recommending")
                try:
                    found, ended = await self.source.search(
                        query,
                        area.center,
                        purpose.purpose,
                        page,
                        **(
                            {"radius": region.radius_m}
                            if region and region.kind == "radius"
                            else {"rect": bounding_rect(area)}
                            if spatial
                            else {}
                        ),
                    )
                except ServiceError:
                    failures += 1
                    more = True
                    break
                for candidate in found:
                    place = candidate.place
                    category = CATEGORY_FILTERS.get(purpose.purpose)
                    if (
                        place.place_id in seen
                        or (spatial and not seoul.contains(place.coordinates))
                        or (
                            direct_distance(region.anchor.coordinates, place.coordinates)
                            > region.radius_m
                            if region and region.kind == "radius"
                            else not area.contains(place.coordinates)
                        )
                        or (category and place.category_code != category)
                    ):
                        continue
                    seen.add(place.place_id)
                    candidates.append(candidate)
                if ended:
                    break
                if not budget:
                    more = True
                    break
            if not budget and target_index + 1 < len(targets):
                more = True
        unique_areas = list({a.area_id: a for a in searched}.values())
        scope = (
            RegionSearchScope(region=region, more_available=more)
            if spatial
            else SearchScope(
                areas=[
                    SearchAreaSnapshot.model_validate(a.model_dump(exclude={"rings", "polygons"}))
                    for a in unique_areas
                ],
                fallback=False,
                more_available=more,
            )
        )
        eligible, route_failures, assessments = [], 0, []
        for candidate in candidates[:15]:
            await checkpoint("recommending")
            route = None
            if conditions.origin:
                try:
                    route = await self.source.route(
                        conditions.origin.coordinates, candidate.place.coordinates
                    )
                except ServiceError:
                    route_failures += 1
                if route is not None and (
                    conditions.max_travel_seconds is not None
                    and route.duration_seconds > conditions.max_travel_seconds
                ):
                    continue
                if route is not None:
                    candidate.duration_seconds = route.duration_seconds
                    candidate.distance_meters = route.distance_meters
                    candidate.route_collected_at = route.collected_at
            assessment = await assess(self.session, candidate, plan.constraints)
            if assessment.truth == "true" and conditions.origin and route is None:
                assessment.truth, assessment.reason_code = "unknown", "ROUTE_UNAVAILABLE"
            assessments.append(assessment)
            if assessment.truth != "true":
                continue
            if conditions.origin:
                traffic = await self.geography.traffic(candidate.place.coordinates)
                candidate.congestion = traffic.grade or "확인 불가"
                candidate.congestion_observed_at = traffic.observed_at
                candidate.congestion_source = traffic.source
                candidate.congestion_reason_code = traffic.reason
            try:
                candidate.parking = await self.source.parking(candidate.place.coordinates)
            except ServiceError:
                candidate.parking = unavailable_parking("DATA_UNAVAILABLE", error=True)
                warnings.append(
                    {"code": "PARKING_UNAVAILABLE", "message": "일부 주차 자료 조회 실패"}
                )
            eligible.append(candidate)
        if len(candidates) > 15:
            scope.more_available = True
        if conditions.origin:
            selected = rank_candidates(eligible)[:3]
            descriptions, reason_source = await explain(None, selected)
            cards = [
                ContextCard.model_validate(card(c, i, text[0], reason_source))
                for i, (c, text) in enumerate(zip(selected, descriptions, strict=True), 1)
            ]
        else:
            # Display order follows bounded regional search, never invented ETA/popularity.
            selected = eligible[:3]
            cards = []
            label = plan.search_area.label if plan.search_area else "서울"
            for i, candidate in enumerate(selected, 1):
                value = ContextCard.model_validate(card(candidate, i))
                value.route.reason_code = "ORIGIN_NOT_SET"
                value.route.freshness = "not_applicable"
                value.reason.text = regional_reason(candidate, label)
                value.reason.source = "fallback"
                cards.append(value)
        scope.more_available |= len(eligible) > len(selected)
        if scope.more_available:
            warnings.append(
                {"code": "SEARCH_INCOMPLETE", "message": "확인한 일부 지역·후보의 결과예요."}
            )
        if failures or route_failures:
            warnings.append(
                {"code": "LOOKUP_PARTIAL_FAILURE", "message": "일부 장소/이동시간 조회 실패"}
            )
        if plan.constraints and any(a.truth == "unknown" for a in assessments):
            warnings.append(
                {
                    "code": "CRITERIA_UNKNOWN",
                    "message": "필수 조건을 확인하지 못한 후보는 제외했어요.",
                }
            )
        outcome = (
            "recommendation"
            if cards
            else "insufficient_evidence"
            if plan.constraints and any(a.truth == "unknown" for a in assessments)
            else "error"
            if failures or (route_failures and not plan.constraints)
            else ("insufficient_evidence" if scope.more_available else "no_candidates")
        )
        return PreparedRecommendation(
            plan=plan,
            selection_mode=mode,
            outcome=outcome,
            cards=cards,
            prepared_at=datetime.now(UTC),
            warnings=warnings,
            search_scope=scope,
            ranking_policy=DEFAULT_POLICY.snapshot() if mode == "driving" and cards else None,
            error=StageError(
                code="RECOMMENDATION_FAILED",
                safe_message="장소나 이동시간 조회에 실패했어요.",
                retryable=True,
            )
            if outcome == "error"
            else None,
            evaluation=EvaluationSummary(assessments=assessments) if plan.constraints else None,
        )

    @staticmethod
    def _missing(plan, mode):
        return PreparedRecommendation(
            plan=plan,
            selection_mode=mode,
            outcome="insufficient_evidence",
            prepared_at=datetime.now(UTC),
            warnings=[
                {
                    "code": "SEARCH_AREA_DATA_UNAVAILABLE",
                    "message": "지정 지역의 경계 자료가 없어요.",
                }
            ],
        )
