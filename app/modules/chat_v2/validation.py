"""Deterministic checks for the first headcount slice; no DB or model calls."""

from app.modules.chat_v2.contracts import (
    ConversationContext,
    HeadcountChangeIntent,
    InterpretationProposal,
    NeedsInputIntent,
    RunRequest,
    UnsupportedIntent,
    ValidatedPlan,
)
from app.modules.chat_v2.results import StageError
from app.schemas.recommendations import Conditions


class StageFailure(Exception):
    """An adapter's explicit, sanitized failure (not a raw provider exception)."""

    def __init__(self, error: StageError):
        self.error = error
        super().__init__(error.code)


def check_request_context(request: RunRequest, context: ConversationContext) -> None:
    """Check a server-supplied snapshot; authentication and live DB reads are upstream."""
    reply_to = request.input.reply_to_clarification_id
    viewed_question = request.viewed_context.clarification_id if request.viewed_context else None
    question = context.pending_question
    if (
        request.request_id != context.identity.request_id
        or request.expected_condition_version != context.condition_version
        or request.expected_state_version != context.state_version
        or any(
            ref is not None and (question is None or ref != question.clarification_id)
            for ref in (reply_to, viewed_question)
        )
    ):
        raise StageFailure(
            StageError(
                code="CONFLICT",
                safe_message=(
                    "대화 상태나 질문이 변경됐어요. 현재 상태를 확인한 뒤 다시 요청해 주세요."
                ),
                retryable=False,
            )
        )


class HeadcountPlanValidator:
    async def validate(
        self,
        request: RunRequest,
        context: ConversationContext,
        proposal: InterpretationProposal,
    ) -> ValidatedPlan | NeedsInputIntent | UnsupportedIntent:
        check_request_context(request, context)
        decision = proposal.decision
        if not isinstance(decision, HeadcountChangeIntent):
            return decision
        if decision.evidence_text not in request.input.text:
            raise StageFailure(
                StageError(
                    code="VALIDATION_FAILED",
                    safe_message="요청 해석의 근거를 확인하지 못했어요. 조건은 변경하지 않았어요.",
                    retryable=False,
                )
            )
        question = context.pending_question
        if request.input.reply_to_clarification_id is not None and question.scope != decision.scope:
            raise StageFailure(
                StageError(
                    code="VALIDATION_FAILED",
                    safe_message="질문과 인원 변경의 적용 범위가 일치하지 않아 반영하지 않았어요.",
                    retryable=False,
                )
            )
        if decision.scope == "request_only":
            return UnsupportedIntent(
                kind="unsupported",
                reason="이번 요청에만 인원을 적용하는 기능은 아직 연결되지 않았어요.",
            )
        values = context.current_conditions.model_dump()
        values["headcount"] = decision.headcount
        conditions = Conditions.model_validate(values)
        if conditions.origin is None or not conditions.origin.supported:
            return NeedsInputIntent(
                kind="needs_input", question="출발지를 알려주세요.", required_field="origin"
            )
        if not conditions.purposes:
            return NeedsInputIntent(
                kind="needs_input",
                question="카페·식사·산책·전시 중 어떤 장소를 찾고 있나요?",
                required_field="purposes",
            )
        return ValidatedPlan(
            context=context.model_copy(deep=True),
            requested_headcount=decision.headcount,
            effective_conditions=conditions,
            changed_fields=[]
            if context.current_conditions.headcount == decision.headcount
            else ["headcount"],
        )
