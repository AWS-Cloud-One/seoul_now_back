"""Local F02 contract examples, not a live interpreter or persistence test."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.modules.chat_v2.contracts import (
    InterpretationProposal,
    ValidatedPlan,
)
from app.modules.chat_v2.results import (
    CommitReceipt,
    FinalizationResult,
    PreparedRecommendation,
    RunSnapshot,
)
from tests.fixtures.chat_v2 import scenario
from tests.fixtures.recommendation_context import NOW


def test_headcount_contracts_connect_without_changing_unmentioned_conditions():
    request, proposal, prepared, receipt = scenario()
    final = FinalizationResult(prepared=prepared, commit={"state": "committed", "receipt": receipt})
    snapshot = RunSnapshot(status="succeeded", finalization=final)

    assert request.input.text == proposal.decision.evidence_text == "셋이요"
    before = prepared.plan.context.current_conditions
    assert before.headcount == 2
    assert receipt.conditions.headcount == 3
    assert before.model_dump(exclude={"headcount"}) == receipt.conditions.model_dump(
        exclude={"headcount"}
    )
    assert RunSnapshot.model_validate_json(snapshot.model_dump_json()) == snapshot
    assert snapshot.finalization.commit.receipt.current_version == 8


def test_model_proposal_cannot_supply_server_fields_or_claim_commit():
    _, proposal, _, _ = scenario()
    payload = proposal.model_dump()
    payload["decision"]["applied"] = True
    payload["run_id"] = str(uuid4())
    with pytest.raises(ValidationError):
        InterpretationProposal.model_validate(payload)


def test_ambiguous_interpretation_does_not_also_contain_a_change():
    payload = {
        "decision": {
            "kind": "needs_input",
            "question": "인원을 몇 명으로 바꿀까요?",
            "required_field": "headcount",
        }
    }
    question = InterpretationProposal.model_validate(payload)
    assert question.decision.kind == "needs_input"
    payload["decision"]["headcount"] = 3
    with pytest.raises(ValidationError):
        InterpretationProposal.model_validate(payload)


def test_f02_plan_rejects_an_unrelated_condition_change():
    _, _, prepared, _ = scenario()
    payload = prepared.plan.model_dump()
    payload["effective_conditions"]["max_travel_seconds"] = 1200
    with pytest.raises(ValidationError, match="declared headcount"):
        ValidatedPlan.model_validate(payload)


def test_same_headcount_does_not_request_another_recommendation():
    _, _, prepared, _ = scenario()
    payload = prepared.plan.model_dump()
    payload["requested_headcount"] = payload["context"]["current_conditions"]["headcount"]
    payload["effective_conditions"] = payload["context"]["current_conditions"]
    payload["changed_fields"] = []
    plan = ValidatedPlan.model_validate(payload)
    assert not plan.requires_recommendation
    with pytest.raises(ValidationError, match="skip recommendation"):
        PreparedRecommendation(plan=plan, outcome="no_candidates", prepared_at=NOW)


def test_prepared_result_cannot_be_published_or_used_as_a_receipt():
    _, _, prepared, _ = scenario()
    with pytest.raises(ValidationError):
        CommitReceipt.model_validate(prepared.model_dump())
    with pytest.raises(ValidationError):
        FinalizationResult(prepared=prepared, commit={"state": "committed"})
    payload = prepared.model_dump()
    payload["result_id"] = str(uuid4())
    with pytest.raises(ValidationError):
        PreparedRecommendation.model_validate(payload)


@pytest.mark.parametrize("wrong", ["run", "conditions", "version"])
def test_receipt_for_a_different_change_cannot_finalize_this_plan(wrong):
    _, _, prepared, receipt = scenario()
    payload = receipt.model_dump()
    if wrong == "run":
        payload["identity"]["run_id"] = uuid4()
    elif wrong == "conditions":
        payload["conditions"]["headcount"] = 4
    else:
        payload["previous_version"], payload["current_version"] = 8, 9
    with pytest.raises(ValidationError, match="exact planned change"):
        FinalizationResult(prepared=prepared, commit={"state": "committed", "receipt": payload})


def test_recommendation_failure_and_confirmed_condition_change_are_separate():
    _, _, prepared, receipt = scenario()
    failure = PreparedRecommendation(
        plan=prepared.plan,
        outcome="error",
        prepared_at=NOW,
        error={
            "code": "RECOMMENDATION_FAILED",
            "safe_message": "이동시간을 확인하지 못했어요.",
            "retryable": True,
        },
    )
    payload = receipt.model_dump()
    payload["result_id"] = None
    final = FinalizationResult(prepared=failure, commit={"state": "committed", "receipt": payload})
    snapshot = RunSnapshot(status="partial", finalization=final)
    assert snapshot.finalization.commit.receipt.conditions.headcount == 3
    assert snapshot.finalization.prepared.error.retryable
    with pytest.raises(ValidationError, match="status=partial"):
        RunSnapshot(status="succeeded", finalization=final)


def test_unknown_commit_cannot_report_success_or_immediate_retry():
    _, _, prepared, _ = scenario()
    commit = {
        "state": "unknown",
        "error": {
            "code": "COMMIT_UNCERTAIN",
            "safe_message": "실제 반영 여부를 확인하고 있어요.",
            "retryable": False,
        },
    }
    final = FinalizationResult(prepared=prepared, commit=commit)
    assert RunSnapshot(status="reconciling", finalization=final)
    with pytest.raises(ValidationError, match="status=reconciling"):
        RunSnapshot(status="succeeded", finalization=final)
    commit["error"]["retryable"] = True
    with pytest.raises(ValidationError, match="reconciled"):
        FinalizationResult(prepared=prepared, commit=commit)


@pytest.mark.parametrize("code,status", [("COMMIT_FAILED", "failed"), ("CANCELLED", "cancelled")])
def test_confirmed_non_commit_keeps_failure_and_cancellation_distinct(code, status):
    _, _, prepared, _ = scenario()
    final = FinalizationResult(
        prepared=prepared,
        commit={
            "state": "not_applied",
            "error": {
                "code": code,
                "safe_message": "변경을 반영하지 않았어요.",
                "retryable": False,
            },
        },
    )
    assert RunSnapshot(status=status, finalization=final)
    with pytest.raises(ValidationError, match=f"status={status}"):
        RunSnapshot(status="succeeded", finalization=final)
