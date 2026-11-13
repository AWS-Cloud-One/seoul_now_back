"""Deterministic responses for the F02 workflow, including early exits.

ChatResponse is also returned by the opt-in V2 HTTP adapter; V1 is unchanged.
Only a confirmed receipt allows public recommendation data in this response.
"""

from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from app.modules.chat_v2.contracts import (
    ConversationContext,
    NeedsInputIntent,
    Nonblank,
    RunIdentity,
    ValidatedPlan,
    Version,
)
from app.modules.chat_v2.dialogue_context import (
    ComparisonResult,
    ContinuationResult,
    DestinationArea,
    LookupResult,
    OriginOption,
)
from app.modules.chat_v2.predicates import EvaluationSummary, Predicate
from app.modules.chat_v2.results import (
    ConfirmedCommit,
    FinalizationResult,
    RunSnapshot,
    StageError,
    UnappliedCommit,
)
from app.schemas.common import ContractModel
from app.schemas.recommendation_result import ContextCard
from app.schemas.recommendations import Conditions


class PublishedRecommendation(ContractModel):
    selection_mode: Literal["driving", "regional"] = "driving"
    search_area: DestinationArea | None = None
    result_id: UUID
    cards: list[ContextCard] = Field(min_length=1, max_length=3)
    effective_conditions: Conditions
    scope: Literal["persistent", "request_only"] = "persistent"
    flow_id: UUID | None = None
    constraints: Predicate | None = None


class ChatResponse(ContractModel):
    identity: RunIdentity
    kind: Literal[
        "result", "clarification", "unchanged", "unsupported", "failure", "answer", "decision"
    ]
    status: Literal[
        "succeeded", "partial", "failed", "cancelled", "interrupted", "reconciling", "waiting_input"
    ]
    answer: Nonblank
    conditions: Conditions | None
    condition_version: Version | None
    snapshot: RunSnapshot | None = None
    recommendation: PublishedRecommendation | None = None
    clarification: NeedsInputIntent | None = None
    error: StageError | None = None
    warnings: list[Nonblank] = Field(default_factory=list)
    lookup: LookupResult | None = None
    origin_options: list[OriginOption] = Field(default_factory=list, max_length=5)
    comparison: ComparisonResult | None = None
    continuation: ContinuationResult | None = None
    evaluation: EvaluationSummary | None = None

    @model_validator(mode="after")
    def response_matches_execution(self):
        if self.comparison is not None and self.kind != "answer":
            raise ValueError("Comparison requires read facts")
        if self.continuation is not None and self.kind != "result":
            raise ValueError("A continuation requires its confirmed primary result")
        if self.kind == "answer":
            if (
                self.lookup is None
                or self.snapshot is not None
                or self.recommendation is not None
                or self.conditions is None
                or self.condition_version is None
                or self.clarification is not None
            ):
                raise ValueError("A read answer needs facts and unchanged conditions")
            expected = (
                "failed"
                if self.lookup.outcome == "error"
                else "partial"
                if self.lookup.outcome == "partial"
                else "succeeded"
            )
            if (
                self.comparison
                and self.comparison.outcome == "incomplete"
                and expected == "succeeded"
            ):
                expected = "partial"
            if self.status != expected:
                raise ValueError("Read status must match lookup outcome")
            return self
        if self.lookup is not None or (self.origin_options and self.kind != "clarification"):
            raise ValueError("Lookup and origin options belong to their response kinds")
        if self.kind == "result":
            if self.snapshot is None or (
                self.clarification is not None
                and (self.continuation is None or self.continuation.status != "waiting_input")
            ):
                raise ValueError("A final response requires its execution snapshot")
            result = self.snapshot.finalization
            context = result.prepared.plan.context
            expected_status = self.snapshot.status
            if self.continuation:
                if result.commit.state != "committed":
                    raise ValueError("Continuation cannot precede commitment")
                expected_status = {
                    "pending": "partial",
                    "failed": "partial",
                    "blocked": "partial",
                    "cancelled": "cancelled",
                    "interrupted": "interrupted",
                    "waiting_input": "waiting_input",
                    "completed": self.snapshot.status,
                }[self.continuation.status]
            if self.identity != context.identity or self.status != expected_status:
                raise ValueError("Response identity and status must match the execution")
            recommendation = None
            if isinstance(result.commit, ConfirmedCommit):
                receipt = result.commit.receipt
                conditions, version = receipt.conditions, receipt.current_version
                error = result.prepared.error
                if receipt.result_id is not None:
                    recommendation = PublishedRecommendation(
                        selection_mode=result.prepared.selection_mode,
                        search_area=getattr(result.prepared.plan, "search_area", None),
                        result_id=receipt.result_id,
                        cards=result.prepared.cards,
                        effective_conditions=result.prepared.plan.effective_conditions,
                        scope=result.prepared.plan.scope,
                        flow_id=getattr(result.prepared.plan, "flow_id", None),
                        constraints=getattr(result.prepared.plan, "constraints", None),
                    )
            elif isinstance(result.commit, UnappliedCommit):
                conditions, version = context.current_conditions, context.condition_version
                error = result.commit.error
            else:
                conditions, version, error = None, None, result.commit.error
            if (
                self.conditions != conditions
                or self.condition_version != version
                or self.recommendation != recommendation
                or self.error != error
                or self.evaluation != result.prepared.evaluation
            ):
                raise ValueError("Response facts must reflect the actual finalization outcome")
        else:
            if self.snapshot is not None or self.recommendation is not None:
                raise ValueError("An early response cannot publish an uncommitted recommendation")
            if self.conditions is None or self.condition_version is None:
                raise ValueError("An early response retains the known current conditions")
            expected = {
                "clarification": "waiting_input",
                "unchanged": "succeeded",
                "unsupported": "succeeded",
                "failure": "failed",
                "decision": "partial"
                if self.evaluation and self.evaluation.guard == "unknown"
                else "succeeded",
            }[self.kind]
            if self.kind == "failure" and self.error is not None:
                expected = {"CANCELLED": "cancelled", "INTERRUPTED": "interrupted"}.get(
                    self.error.code, expected
                )
            if self.status != expected:
                raise ValueError("Early response status must match its kind")
            if (self.kind == "clarification") != (self.clarification is not None):
                raise ValueError("Only an input question carries clarification data")
            if (self.kind in {"unsupported", "failure"}) != (self.error is not None):
                raise ValueError("Failure and unsupported responses require a reason")
            if self.kind == "unsupported" and self.error.code != "UNSUPPORTED":
                raise ValueError("Unsupported is not an internal processing failure")
            if self.kind == "decision" and (
                self.evaluation is None or self.evaluation.guard not in {"false", "unknown"}
            ):
                raise ValueError("A skipped change needs the server's predicate decision")
        return self


class DefaultResponseComposer:
    """Render only server-owned status and facts; no language-model invocation."""

    def compose(self, result: FinalizationResult) -> ChatResponse:
        plan = result.prepared.plan
        context = plan.context
        recommendation = None
        if isinstance(result.commit, ConfirmedCommit):
            receipt = result.commit.receipt
            conditions, version = receipt.conditions, receipt.current_version
            error = result.prepared.error
            answer = (
                "여행 기본 조건은 유지하고 이번 추천에만 조건을 적용했어요."
                if plan.scope == "request_only"
                else f"인원을 {conditions.headcount}명으로 변경했어요."
                if plan.feature_id == "F02"
                else "요청한 조건을 반영했어요."
                if plan.changed_fields
                or (getattr(plan, "search_area", None) != context.search_area)
                else "현재 조건을 유지했어요."
            )
            match result.prepared.outcome:
                case "recommendation":
                    status = "succeeded"
                    recommendation = PublishedRecommendation(
                        selection_mode=result.prepared.selection_mode,
                        search_area=getattr(result.prepared.plan, "search_area", None),
                        result_id=receipt.result_id,
                        cards=result.prepared.cards,
                        effective_conditions=plan.effective_conditions,
                        scope=plan.scope,
                        flow_id=getattr(plan, "flow_id", None),
                        constraints=getattr(plan, "constraints", None),
                    )
                    area = getattr(plan, "search_area", None)
                    label = area.label if area else "서울"
                    answer += (
                        f" 새 조건으로 {len(recommendation.cards)}곳을 추천했어요."
                        if plan.feature_id == "F02"
                        else f" {label} 탐색 범위에서 {len(recommendation.cards)}곳을 추천했어요."
                    )
                    if result.prepared.selection_mode == "regional":
                        answer += (
                            " 지역·방문 목적에 맞는 후보이며, "
                            "이동시간과 주행 순위는 계산하지 않았어요."
                        )
                case "no_candidates":
                    status = "succeeded"
                    answer += " 확인한 범위에서 조건에 맞는 후보를 찾지 못했어요."
                case "insufficient_evidence":
                    status = "partial"
                    answer += " 자료가 부족해 새 후보의 조건 충족 여부를 확인하지 못했어요."
                case "error":
                    status = "partial"
                    answer += " 새 추천 조회에 실패했어요."
                    if plan.feature_id == "F02":
                        answer += " 인원 변경은 반영됐어요."
        elif isinstance(result.commit, UnappliedCommit):
            conditions, version = context.current_conditions, context.condition_version
            error = result.commit.error
            status = "cancelled" if error.code == "CANCELLED" else "failed"
            answer = "변경을 반영하지 않았어요. " + error.safe_message
        else:
            conditions, version = None, None
            error = result.commit.error
            status = "reconciling"
            answer = "변경의 실제 반영 여부를 확인해야 해요. 같은 변경을 다시 실행하지 않아요."
        if result.prepared.evaluation and result.prepared.evaluation.branch == "alternative":
            answer += " 요청에서 허용한 대안 조건을 적용했어요."
        if any(w.code == "CRITERIA_UNKNOWN" for w in result.prepared.warnings):
            answer += " 필수 조건을 확인하지 못한 후보는 제외했어요."
        snapshot = RunSnapshot(status=status, finalization=result)
        return ChatResponse(
            identity=context.identity,
            kind="result",
            status=status,
            answer=answer,
            conditions=conditions,
            condition_version=version,
            snapshot=snapshot,
            recommendation=recommendation,
            error=error,
            warnings=[warning.code for warning in result.prepared.warnings],
            evaluation=result.prepared.evaluation,
        )

    def needs_input(self, context: ConversationContext, question: NeedsInputIntent) -> ChatResponse:
        return self._early(
            context,
            kind="clarification",
            status="waiting_input",
            answer=question.question,
            clarification=question,
        )

    def unchanged(self, plan: ValidatedPlan) -> ChatResponse:
        return self._early(
            plan.context,
            kind="unchanged",
            status="succeeded",
            answer=f"현재 인원은 이미 {plan.requested_headcount}명이에요. 조건을 유지했어요.",
        )

    def unsupported(self, context: ConversationContext, reason: str) -> ChatResponse:
        return self._early(
            context,
            kind="unsupported",
            status="succeeded",
            answer=reason,
            error=StageError(code="UNSUPPORTED", safe_message=reason, retryable=False),
        )

    def failed(self, context: ConversationContext, error: StageError) -> ChatResponse:
        return self._early(
            context,
            kind="failure",
            status={"CANCELLED": "cancelled", "INTERRUPTED": "interrupted"}.get(
                error.code, "failed"
            ),
            answer=error.safe_message,
            error=error,
        )

    @staticmethod
    def _early(context, **fields):
        return ChatResponse(
            identity=context.identity,
            conditions=context.current_conditions,
            condition_version=context.condition_version,
            **fields,
        )
