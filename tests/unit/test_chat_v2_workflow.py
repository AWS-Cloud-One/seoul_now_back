"""Execute the real workflow with explicit test-only interpreter/service/storage doubles."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.modules.chat_v2.contracts import InterpretationProposal
from app.modules.chat_v2.responses import ChatResponse, DefaultResponseComposer
from app.modules.chat_v2.results import (
    FinalizationResult,
    PreparedRecommendation,
    StageError,
)
from app.modules.chat_v2.validation import HeadcountPlanValidator, StageFailure
from app.modules.chat_v2.workflow import HeadcountChangeWorkflow
from tests.fixtures.chat_v2 import scenario
from tests.fixtures.recommendation_context import NOW


@pytest.fixture
def flow():
    request, proposal, template, receipt = scenario()
    calls = []

    async def interpret(request, context):
        calls.append("interpret")
        return proposal

    async def prepare(plan):
        calls.append("prepare")
        return PreparedRecommendation(
            plan=plan, outcome="recommendation", cards=template.cards, prepared_at=NOW
        )

    async def finalize(prepared):
        calls.append("finalize")
        data = receipt.model_dump()
        data["result_id"] = receipt.result_id if prepared.outcome == "recommendation" else None
        return FinalizationResult(prepared=prepared, commit={"state": "committed", "receipt": data})

    class Validator(HeadcountPlanValidator):
        async def validate(self, *args):
            calls.append("validate")
            return await super().validate(*args)

    class Composer(DefaultResponseComposer):
        def compose(self, result):
            calls.append("respond")
            return super().compose(result)

    interpreter = SimpleNamespace(interpret=AsyncMock(side_effect=interpret))
    preparer = SimpleNamespace(prepare=AsyncMock(side_effect=prepare))
    finalizer = SimpleNamespace(finalize=AsyncMock(side_effect=finalize))
    composer = Composer()
    workflow = HeadcountChangeWorkflow(
        interpreter=interpreter,
        validator=Validator(),
        preparer=preparer,
        finalizer=finalizer,
        responses=composer,
        clock=lambda: NOW,
    )
    return SimpleNamespace(
        request=request,
        context=template.plan.context,
        proposal=proposal,
        template=template,
        receipt=receipt,
        calls=calls,
        workflow=workflow,
        interpreter=interpreter,
        preparer=preparer,
        finalizer=finalizer,
        composer=composer,
    )


def execute(flow):
    return asyncio.run(flow.workflow.execute(flow.request, flow.context))


def assert_no_preparation_or_storage(flow):
    flow.preparer.prepare.assert_not_awaited()
    flow.finalizer.finalize.assert_not_awaited()


def test_request_runs_through_all_components_and_returns_an_answer(flow):
    result = execute(flow)
    assert flow.calls == ["interpret", "validate", "prepare", "finalize", "respond"]
    assert result.status == "succeeded"
    assert result.answer == "인원을 3명으로 변경했어요. 새 조건으로 1곳을 추천했어요."
    assert result.conditions.headcount == 3 and result.condition_version == 8
    assert result.recommendation.result_id == flow.receipt.result_id
    assert result.recommendation.cards == flow.template.cards
    assert flow.context.current_conditions.headcount == 2  # caller's snapshot is untouched
    flow.finalizer.finalize.assert_awaited_once()
    assert ChatResponse.model_validate_json(result.model_dump_json()) == result


def test_ambiguous_input_returns_question_without_executing_change(flow):
    flow.interpreter.interpret.side_effect = None
    flow.interpreter.interpret.return_value = InterpretationProposal(
        decision={
            "kind": "needs_input",
            "question": "인원을 몇 명으로 바꿀까요?",
            "required_field": "headcount",
        }
    )
    result = execute(flow)
    assert result.status == "waiting_input"
    assert result.answer == result.clarification.question
    assert result.conditions.headcount == 2
    assert_no_preparation_or_storage(flow)


def test_unsupported_request_returns_notice_without_executing_change(flow):
    flow.interpreter.interpret.side_effect = None
    flow.interpreter.interpret.return_value = InterpretationProposal(
        decision={"kind": "unsupported", "reason": "예약 기능은 지원하지 않아요."}
    )
    result = execute(flow)
    assert result.kind == "unsupported" and result.error.code == "UNSUPPORTED"
    assert result.conditions.headcount == 2
    assert_no_preparation_or_storage(flow)


def test_same_headcount_skips_preparation_and_storage(flow):
    flow.request.input.text = "두 명이요"
    flow.proposal.decision.headcount = 2
    flow.proposal.decision.evidence_text = "두 명이요"
    result = execute(flow)
    assert result.kind == "unchanged" and result.condition_version == 7
    assert "이미 2명" in result.answer
    assert_no_preparation_or_storage(flow)


def test_stale_request_is_rejected_before_model_or_service_calls(flow):
    flow.request.expected_condition_version = 6
    result = execute(flow)
    assert result.status == "failed" and result.error.code == "CONFLICT"
    flow.interpreter.interpret.assert_not_awaited()
    assert_no_preparation_or_storage(flow)


def test_request_only_scope_is_not_silently_saved_as_a_persistent_change(flow):
    flow.context.pending_question.scope = "request_only"
    flow.proposal.decision.scope = "request_only"
    result = execute(flow)
    assert result.kind == "unsupported"
    assert result.conditions.headcount == 2
    assert_no_preparation_or_storage(flow)


def test_missing_origin_returns_required_input_instead_of_preparing(flow):
    flow.context.current_conditions.origin = None
    result = execute(flow)
    assert result.status == "waiting_input" and result.clarification.required_field == "origin"
    assert_no_preparation_or_storage(flow)


@pytest.mark.parametrize("mode", ["timeout", "invalid_output"])
def test_interpretation_failure_is_not_reported_as_user_ambiguity(flow, mode):
    if mode == "timeout":
        flow.interpreter.interpret.side_effect = TimeoutError("private provider detail")
    else:
        flow.interpreter.interpret.side_effect = None
        flow.interpreter.interpret.return_value = {"decision": {"kind": "change_headcount"}}
    result = execute(flow)
    assert result.error.code == "INTERPRETATION_FAILED"
    assert result.status == "failed" and result.clarification is None
    assert "private" not in result.answer
    assert_no_preparation_or_storage(flow)


def test_known_recommendation_failure_still_finalizes_valid_change_once(flow):
    flow.preparer.prepare.side_effect = StageFailure(
        StageError(
            code="RECOMMENDATION_FAILED", safe_message="길찾기 조회에 실패했어요.", retryable=True
        )
    )
    result = execute(flow)
    assert result.status == "partial" and result.conditions.headcount == 3
    assert result.error.code == "RECOMMENDATION_FAILED" and result.recommendation is None
    assert "인원 변경은 반영" in result.answer
    flow.finalizer.finalize.assert_awaited_once()


def test_normal_empty_recommendation_is_distinct_from_query_failure(flow):
    flow.preparer.prepare.side_effect = lambda plan: PreparedRecommendation(
        plan=plan, outcome="no_candidates", prepared_at=NOW
    )
    result = execute(flow)
    assert result.status == "succeeded" and result.conditions.headcount == 3
    assert result.error is None and result.recommendation is None
    assert "후보를 찾지 못했어요" in result.answer


def test_preparation_for_another_run_never_reaches_finalizer(flow):
    wrong = flow.template.model_copy(deep=True)
    wrong.plan.context.identity.run_id = uuid4()
    flow.preparer.prepare.side_effect = None
    flow.preparer.prepare.return_value = wrong
    result = execute(flow)
    assert result.error.code == "INTERNAL_ERROR"
    flow.finalizer.finalize.assert_not_awaited()


@pytest.mark.parametrize("mode", ["exception", "wrong_receipt"])
def test_uncertain_finalization_cannot_publish_success_or_retry(flow, mode):
    if mode == "exception":
        flow.finalizer.finalize.side_effect = TimeoutError("private database detail")
    else:
        data = flow.receipt.model_dump()
        data["identity"]["run_id"] = uuid4()
        flow.finalizer.finalize.side_effect = lambda prepared: {
            "prepared": prepared,
            "commit": {"state": "committed", "receipt": data},
        }
    result = execute(flow)
    assert result.status == "reconciling" and result.error.code == "COMMIT_UNCERTAIN"
    assert not result.error.retryable
    assert result.conditions is None and result.condition_version is None
    assert result.recommendation is None
    assert "private" not in result.answer
    flow.finalizer.finalize.assert_awaited_once()


def test_confirmed_unapplied_result_keeps_old_conditions(flow):
    flow.finalizer.finalize.side_effect = lambda prepared: FinalizationResult(
        prepared=prepared,
        commit={
            "state": "not_applied",
            "error": {
                "code": "COMMIT_FAILED",
                "safe_message": "저장을 완료하지 못했어요.",
                "retryable": True,
            },
        },
    )
    result = execute(flow)
    assert result.status == "failed" and result.conditions.headcount == 2
    assert result.condition_version == 7 and result.recommendation is None


def test_response_failure_keeps_confirmed_result_without_repeating_work(flow):
    def broken(result):
        raise RuntimeError("response generation failed")

    flow.composer.compose = broken
    result = execute(flow)
    assert result.status == "succeeded" and result.conditions.headcount == 3
    assert result.recommendation.result_id == flow.receipt.result_id
    assert result.warnings == ["RESPONSE_FALLBACK"]
    flow.interpreter.interpret.assert_awaited_once()
    flow.preparer.prepare.assert_awaited_once()
    flow.finalizer.finalize.assert_awaited_once()


def test_response_for_a_different_run_is_replaced_with_the_verified_result(flow):
    def wrong(result):
        other = result.model_copy(deep=True)
        wrong_id = uuid4()
        other.prepared.plan.context.identity.run_id = wrong_id
        other.commit.receipt.identity.run_id = wrong_id
        return DefaultResponseComposer().compose(other)

    flow.composer.compose = wrong
    result = execute(flow)
    assert result.identity == flow.context.identity
    assert result.warnings == ["RESPONSE_FALLBACK"]
    flow.finalizer.finalize.assert_awaited_once()
