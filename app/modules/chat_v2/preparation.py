"""Adapt existing compute-only recommendation services to the V2 preparation port.

The caller supplies a private workspace from its execution owner. This adapter
does not acquire ownership, publish progress, call execute(), or commit a result.
The legacy engine's applied flag and generated result ID are not storage evidence.
"""

from collections.abc import Callable
from datetime import UTC, datetime

from app.core.exceptions import ServiceError
from app.modules.chat_v2.contracts import RecommendationPlan, RunIdentity, ValidatedPlan
from app.modules.chat_v2.results import PreparedRecommendation, StageError
from app.modules.chat_v2.validation import StageFailure
from app.modules.recommendation.progress import RecommendationWork, check_current, fingerprint
from app.schemas.recommendation_result import ContextResult, SearchAreaSnapshot, SearchScope
from app.schemas.recommendations import RecommendationRequest
from app.services.recommendations import RecommendationService


def fail_contract():
    return StageFailure(
        StageError(
            code="INTERNAL_ERROR",
            safe_message="추천 준비 결과의 조건이나 실행 정보를 확인하지 못했어요.",
            retryable=False,
        )
    )


class ExistingRecommendationPreparer:
    def __init__(
        self,
        service: RecommendationService,
        *,
        identity: RunIdentity,
        work: RecommendationWork,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self.service = service
        self.identity = identity.model_copy(deep=True)
        self.work = work
        self.clock = clock

    async def prepare(self, plan: ValidatedPlan) -> PreparedRecommendation:
        plan_type = RecommendationPlan if plan.feature_id == "F01" else ValidatedPlan
        plan = plan_type.model_validate(plan.model_dump())
        context = plan.context
        if context.identity != self.identity or not plan.requires_recommendation:
            raise fail_contract()
        request = RecommendationRequest(
            request_id=context.identity.request_id,
            trip_id=context.identity.trip_id,
            condition_version=context.condition_version,
            current_conditions=context.current_conditions.model_copy(deep=True),
            conditions_patch={
                key: plan.effective_conditions.model_dump()[key] for key in plan.changed_fields
            },
            excluded_place_ids=sorted(self.work.excluded),
        )
        try:
            check_current(request, self.work)
            if self.work.invalidated:
                raise ServiceError("CONTEXT_EXPIRED", "실행 상태가 무효화되었습니다.")
        except ServiceError:
            raise StageFailure(
                StageError(
                    code="CONFLICT",
                    safe_message="현재 여행 상태를 다시 확인해야 해요.",
                    retryable=False,
                )
            ) from None

        # Deliberately bypass _dispatch()/execution.run(): those can commit and publish.
        outcome = await self.service.prepare(request, self.work)
        data = outcome.response
        update = data.condition_update
        if (
            data.trip_id != context.identity.trip_id
            or data.base_condition_version != context.condition_version
            or (
                update.applied
                and (
                    update.conditions != plan.effective_conditions
                    or update.version != context.condition_version + bool(plan.changed_fields)
                    or [change.field for change in update.changes] != plan.changed_fields
                )
            )
            or (
                not update.applied
                and (
                    update.conditions != context.current_conditions
                    or update.version != context.condition_version
                    or update.changes
                )
            )
        ):
            raise fail_contract()

        details = {"warnings": data.warnings, "search_scope": self._search_scope(plan)}
        if outcome.failure is not None:
            failure = outcome.failure
            if failure.code == "INTERNAL_ERROR":
                raise fail_contract()
            if failure.code in {"VALIDATION_ERROR", "ORIGIN_OUT_OF_SERVICE_AREA"}:
                raise StageFailure(
                    StageError(
                        code="VALIDATION_FAILED", safe_message=failure.message, retryable=False
                    )
                )
            return self._result(
                plan,
                "error",
                error=StageError(
                    code="RECOMMENDATION_FAILED",
                    safe_message=failure.message,
                    retryable=failure.retryable
                    if failure.retryable is not None
                    else failure.code
                    in {
                        "RATE_LIMITED",
                        "PROVIDER_ERROR",
                        "PROVIDER_TIMEOUT",
                        "DATA_UNAVAILABLE",
                        "SERVICE_UNAVAILABLE",
                    },
                ),
                **details,
            )
        if not update.applied:
            # A new geography/condition question must not silently approve the old plan.
            raise StageFailure(
                StageError(
                    code="VALIDATION_FAILED",
                    safe_message="추천에 필요한 조건을 다시 확인해야 해요.",
                    retryable=False,
                )
            )

        if data.kind in {"recommendation", "general_candidates"}:
            computed = ContextResult.model_validate(data.result)
            if (
                computed.conditions != plan.effective_conditions
                or computed.context_token is not None
                or computed.context_expires_at is not None
            ):
                raise fail_contract()
            details.update(ranking_policy=computed.ranking_policy)
            if computed.search_scope is not None:
                details["search_scope"] = computed.search_scope
            if data.kind == "recommendation":
                # Preparation-local pagination tokens are not durable public actions.
                for card in computed.cards:
                    card.parking.next_cursor = None
                return self._result(plan, "recommendation", cards=computed.cards, **details)
            return self._result(
                plan,
                "error",
                error=StageError(
                    code="RECOMMENDATION_FAILED",
                    safe_message="후보의 이동시간을 확인하지 못했어요.",
                    retryable=True,
                ),
                **details,
            )
        if data.kind != "no_candidates":
            raise fail_contract()
        incomplete = {
            "SEARCH_INCOMPLETE",
            "SEARCH_PROVIDER_LIMIT",
            "SEARCH_AREA_DATA_INCOMPLETE",
            "ROUTE_PARTIAL_FAILURE",
            "ROUTE_ALL_FAILED",
        }
        status = (
            "insufficient_evidence"
            if incomplete.intersection(warning.code for warning in data.warnings)
            else "no_candidates"
        )
        return self._result(plan, status, **details)

    def _result(self, plan, outcome, **fields):
        return PreparedRecommendation(
            plan=plan, outcome=outcome, prepared_at=self.clock(), **fields
        )

    def _search_scope(self, plan):
        progress = self.work.search
        if progress.fingerprint != fingerprint(plan.effective_conditions.model_dump(mode="json")):
            return None
        areas = progress.areas or []
        targets = {
            (purpose.purpose, area.area_id if area else "origin")
            for area in areas or [None]
            for purpose in plan.effective_conditions.purposes
        }
        return SearchScope(
            areas=[
                SearchAreaSnapshot.model_validate(area.model_dump(exclude={"rings", "polygons"}))
                for area in areas
            ],
            fallback=not bool(areas),
            more_available=bool(progress.remaining) or not targets.issubset(progress.ended),
        )
