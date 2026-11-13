"""Predetermined F02 inputs and outputs; no model, API or database calls."""

from datetime import timedelta
from uuid import uuid4

from app.modules.chat_v2.contracts import (
    ConversationContext,
    InterpretationProposal,
    RunIdentity,
    RunRequest,
    ValidatedPlan,
)
from app.modules.chat_v2.results import CommitReceipt, PreparedRecommendation
from tests.fixtures.recommendation_context import NOW, response


def scenario():
    """Provide predetermined interpretation and storage evidence; do not execute either."""
    previous = response()
    request = RunRequest(
        request_id=uuid4(),
        expected_condition_version=7,
        expected_state_version=18,
        input={
            "text": "셋이요",
            "reply_to_clarification_id": "headcount-question",
        },
    )
    context = ConversationContext(
        identity=RunIdentity(
            conversation_id=uuid4(),
            trip_id=previous.trip_id,
            run_id=uuid4(),
            request_id=request.request_id,
        ),
        condition_version=7,
        state_version=18,
        current_conditions=previous.condition_update.conditions,
        pending_question={
            "clarification_id": "headcount-question",
            "scope": "persistent",
            "question": "몇 명인가요?",
        },
    )
    proposal = InterpretationProposal(
        decision={
            "kind": "change_headcount",
            "headcount": 3,
            "scope": "persistent",
            "evidence_text": request.input.text,
        }
    )
    effective = context.current_conditions.model_dump()
    effective["headcount"] = proposal.decision.headcount
    plan = ValidatedPlan(
        context=context,
        requested_headcount=proposal.decision.headcount,
        effective_conditions=effective,
        changed_fields=["headcount"],
    )
    prepared = PreparedRecommendation(
        plan=plan,
        outcome="recommendation",
        cards=previous.result["cards"],
        prepared_at=NOW,
    )
    receipt = CommitReceipt(
        identity=context.identity,
        previous_version=7,
        current_version=8,
        conditions=plan.effective_conditions,
        changed_fields=["headcount"],
        result_id=uuid4(),
        committed_at=NOW + timedelta(seconds=1),
    )
    return request, proposal, prepared, receipt
