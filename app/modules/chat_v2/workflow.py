"""Executable, dependency-injected F02 workflow; no external adapters by default.

This is a single invocation skeleton, not a durable job runner. Authentication,
replay, leases, cancellation APIs and DB reconciliation belong to later adapters.
Test doubles live under tests, never in this production module.
"""

from collections.abc import Callable
from datetime import UTC, datetime

from pydantic import BaseModel

from app.modules.chat_v2.contracts import (
    ConversationContext,
    HeadcountChangeIntent,
    InterpretationProposal,
    NeedsInputIntent,
    RunRequest,
    UnsupportedIntent,
    ValidatedPlan,
)
from app.modules.chat_v2.ports import (
    Finalizer,
    Interpreter,
    PlanValidator,
    RecommendationPreparer,
    ResponseComposer,
)
from app.modules.chat_v2.responses import ChatResponse, DefaultResponseComposer
from app.modules.chat_v2.results import FinalizationResult, PreparedRecommendation, StageError
from app.modules.chat_v2.validation import (
    HeadcountPlanValidator,
    StageFailure,
    check_request_context,
)


def _validated[T: BaseModel](contract: type[T], value) -> T:
    """Revalidate adapter output, including previously constructed/mutated models."""
    return contract.model_validate(value.model_dump() if isinstance(value, BaseModel) else value)


class HeadcountChangeWorkflow:
    def __init__(
        self,
        *,
        interpreter: Interpreter,
        preparer: RecommendationPreparer,
        finalizer: Finalizer,
        validator: PlanValidator | None = None,
        responses: ResponseComposer | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self.interpreter, self.preparer, self.finalizer = interpreter, preparer, finalizer
        self.validator = validator if validator is not None else HeadcountPlanValidator()
        self.responses = responses if responses is not None else DefaultResponseComposer()
        self.clock = clock

    def _respond(self, method, *args) -> ChatResponse:
        try:
            response = _validated(ChatResponse, getattr(self.responses, method)(*args))
            source = args[0]
            if isinstance(source, FinalizationResult):
                context = source.prepared.plan.context
                if response.snapshot is None or response.snapshot.finalization != source:
                    raise ValueError("Response replaced the finalization outcome")
            else:
                context = source.context if isinstance(source, ValidatedPlan) else source
                if (
                    response.snapshot is not None
                    or response.conditions != context.current_conditions
                    or response.condition_version != context.condition_version
                ):
                    raise ValueError("Early response replaced current conditions")
            if response.identity != context.identity:
                raise ValueError("Response belongs to a different execution")
            return response
        except Exception:
            # A response failure must not repeat interpretation, preparation or commit.
            fallback = getattr(DefaultResponseComposer(), method)(*args)
            fallback.warnings.append("RESPONSE_FALLBACK")
            return fallback

    async def execute(self, request: RunRequest, context: ConversationContext) -> ChatResponse:
        request = _validated(RunRequest, request)
        context = _validated(ConversationContext, context)
        try:
            check_request_context(request, context)
        except StageFailure as exc:
            return self._respond("failed", context, exc.error)

        try:
            proposal = _validated(
                InterpretationProposal,
                await self.interpreter.interpret(
                    request.model_copy(deep=True), context.model_copy(deep=True)
                ),
            )
        except StageFailure as exc:
            return self._respond("failed", context, exc.error)
        except Exception:
            return self._respond(
                "failed",
                context,
                StageError(
                    code="INTERPRETATION_FAILED",
                    safe_message="요청을 해석하는 처리에 실패했어요. 조건은 변경하지 않았어요.",
                    retryable=True,
                ),
            )

        try:
            decision = await self.validator.validate(
                request.model_copy(deep=True),
                context.model_copy(deep=True),
                proposal.model_copy(deep=True),
            )
            if isinstance(decision, NeedsInputIntent):
                return self._respond("needs_input", context, _validated(NeedsInputIntent, decision))
            if isinstance(decision, UnsupportedIntent):
                return self._respond(
                    "unsupported", context, _validated(UnsupportedIntent, decision).reason
                )
            plan = _validated(ValidatedPlan, decision)
            intent = proposal.decision
            if (
                not isinstance(intent, HeadcountChangeIntent)
                or plan.context != context
                or plan.requested_headcount != intent.headcount
                or plan.scope != intent.scope
            ):
                raise ValueError("Validator returned a plan for a different request or intent")
        except StageFailure as exc:
            return self._respond("failed", context, exc.error)
        except Exception:
            return self._respond(
                "failed",
                context,
                StageError(
                    code="VALIDATION_FAILED",
                    safe_message="인원 변경안을 검증하지 못했어요. 조건은 변경하지 않았어요.",
                    retryable=False,
                ),
            )

        if not plan.requires_recommendation:
            return self._respond("unchanged", plan)

        try:
            prepared = _validated(
                PreparedRecommendation, await self.preparer.prepare(plan.model_copy(deep=True))
            )
            if prepared.plan != plan:
                raise ValueError("Preparation returned a different plan")
        except (StageFailure, TimeoutError) as exc:
            if isinstance(exc, StageFailure) and exc.error.code != "RECOMMENDATION_FAILED":
                return self._respond("failed", context, exc.error)
            # A known lookup failure does not discard an unconditional valid change.
            prepared = PreparedRecommendation(
                plan=plan,
                outcome="error",
                prepared_at=self.clock(),
                error=exc.error
                if isinstance(exc, StageFailure)
                else StageError(
                    code="RECOMMENDATION_FAILED",
                    safe_message="새 추천 조회 시간이 초과됐어요.",
                    retryable=True,
                ),
            )
        except Exception:
            return self._respond(
                "failed",
                context,
                StageError(
                    code="INTERNAL_ERROR",
                    safe_message="추천 준비 결과를 확인하지 못했어요. 변경은 반영하지 않았어요.",
                    retryable=False,
                ),
            )

        try:
            final = _validated(
                FinalizationResult, await self.finalizer.finalize(prepared.model_copy(deep=True))
            )
            if final.prepared != prepared:
                raise ValueError("Finalizer returned a different prepared result")
        except Exception:
            # Once storage was invoked, an exception or malformed response cannot
            # establish that nothing was committed. Never retry it here.
            final = FinalizationResult(
                prepared=prepared,
                commit={
                    "state": "unknown",
                    "error": StageError(
                        code="COMMIT_UNCERTAIN",
                        safe_message="변경의 실제 반영 여부를 확인해야 해요.",
                        retryable=False,
                    ),
                },
            )
        return self._respond("compose", final)
