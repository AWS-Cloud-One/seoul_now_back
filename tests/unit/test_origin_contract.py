import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.core.exceptions import ServiceError
from app.modules.ai.interpretation import Interpretation, interpret
from app.schemas.origin import (
    OriginAction,
    OriginCandidate,
    OriginInterpretationContext,
    OriginReply,
)
from app.schemas.pending import ChatRequest
from app.schemas.recommendations import Clarification
from tests.fixtures.origin import ORIGIN_TOKEN, origin_choice, origin_clarification
from tests.unit.recommendation.test_progress import request


def chat_request(**changes):
    return ChatRequest(
        **request().model_dump(exclude={"conditions_patch"}),
        **({"message": "인천 시청역에서 출발"} | changes),
    )


def hint(source="message", text="인천", **changes):
    return {
        "kind": "region",
        "text": text,
        "source": source,
        "quote": text,
        "message_index": 0 if source == "recent_user_message" else None,
    } | changes


@pytest.mark.parametrize(
    "mode,count", [("confirm", 1), ("confirm", 5), ("choose", 2), ("refine", 0)]
)
def test_origin_question_round_trip_preserves_clear_without_applying_origin(mode, count):
    wire = origin_clarification(mode, count)
    value = Clarification.model_validate(wire)
    assert value.model_dump(mode="json") == wire
    assert value.pending_patch.model_dump(exclude_unset=True) == {"max_travel_seconds": None}
    assert "origin" not in value.pending_patch.model_fields_set


def test_legacy_question_and_chat_need_no_new_origin_fields():
    pending = {
        "question": "인원?",
        "required_fields": ["headcount"],
        "pending_patch": {},
        "choices": [],
    }
    value = chat_request(pending_clarification=pending)
    assert value.origin_action is None
    assert value.pending_clarification.origin_context is None
    assert Interpretation(intent="recommendation").patch().model_dump(exclude_unset=True) == {}


@pytest.mark.parametrize(
    "fault",
    [
        "missing_origin",
        "duplicate",
        "wrong_preferred",
        "bad_coordinates",
        "fake_confidence",
        "too_many",
        "choose_one",
        "refine_with_choice",
        "no_timezone",
        "choose_preferred",
        "untyped_choice",
    ],
)
def test_origin_questions_reject_inconsistent_or_unusable_display_choices(fault):
    body = origin_clarification()
    if fault == "missing_origin":
        body["required_fields"] = ["purposes"]
    elif fault == "duplicate":
        body["choices"].append(deepcopy(body["choices"][0]))
    elif fault == "wrong_preferred":
        body["origin_context"]["preferred_candidate_id"] = "not-in-list"
    elif fault == "bad_coordinates":
        body["choices"][0]["value"]["location"]["coordinates"]["latitude"] = 91
    elif fault == "fake_confidence":
        body["choices"][0]["value"]["confidence"] = 0.99
    elif fault == "too_many":
        body = origin_clarification(count=6)
    elif fault == "choose_one":
        body = origin_clarification("choose", 1)
    elif fault == "refine_with_choice":
        body = origin_clarification("refine", 1)
    elif fault == "no_timezone":
        body["origin_context"]["expires_at"] = "2030-01-01T00:05:00"
    elif fault == "choose_preferred":
        body = origin_clarification("choose", 2)
        body["origin_context"]["preferred_candidate_id"] = "origin-1"
    else:
        body["choices"][0]["value"] = "시청역"
    with pytest.raises(ValidationError):
        Clarification.model_validate(body)


@pytest.mark.parametrize(
    "action",
    [
        {"action": "select"},
        {"action": "select", "candidate_id": " "},
        {"action": "restart", "candidate_id": "origin-1"},
        {"action": "show_alternatives", "candidate_id": "origin-1"},
        {"action": "select", "candidate_id": "origin-1", "coordinates": {"latitude": 37}},
    ],
)
def test_selection_only_accepts_identifiers_for_its_action(action):
    with pytest.raises(ValidationError):
        OriginAction.model_validate({"context_token": ORIGIN_TOKEN} | action)


@pytest.mark.parametrize("action", ["select", "show_alternatives", "restart"])
def test_structured_selection_can_reference_server_context_without_echoing_display_data(action):
    body = {"action": action, "context_token": ORIGIN_TOKEN}
    if action == "select":
        body["candidate_id"] = "origin-1"
    result = chat_request(origin_action=body)
    assert result.origin_action.action == action and result.pending_clarification is None


def test_selection_rejects_conflicting_question_and_result_target():
    action = {"action": "select", "candidate_id": "origin-1", "context_token": ORIGIN_TOKEN}
    pending = origin_clarification()
    pending["origin_context"]["context_token"] = "x" * 43
    with pytest.raises(ValidationError):
        chat_request(origin_action=action, pending_clarification=pending)
    with pytest.raises(ValidationError):
        chat_request(
            origin_action=action,
            target={"result_id": "result", "place_id": "place", "context_token": "other"},
        )


def test_provider_order_is_preserved_separately_from_semantic_evidence():
    candidate = OriginCandidate(
        candidate_id="origin-1",
        source="kakao_keyword",
        provider_id="provider-123",
        location=origin_choice()["value"]["location"],
        category_code="SW8",
        category_name="교통 > 지하철역",
        provider_order=3,
        match={"name": "exact", "place_type": "match", "explicit_matches": ["region"]},
    )
    assert candidate.provider_order == 3
    assert "provider_order" not in candidate.match.model_dump()
    with pytest.raises(ValidationError):
        OriginCandidate.model_validate(candidate.model_dump() | {"confidence": 0.99})


def test_hints_and_selection_never_leak_into_conditions_patch():
    parsed = Interpretation(
        intent="recommendation",
        origin_hints=[hint()],
        origin_reply={"action": "select", "candidate_index": 2},
        max_travel_seconds=1200,
        clear_fields=["headcount"],
        clarification_fields=["group_type"],
    )
    assert parsed.patch().model_dump(exclude_unset=True) == {
        "max_travel_seconds": 1200,
        "headcount": None,
    }


@pytest.mark.parametrize("candidate_index", [True, "2", 0, 6])
def test_candidate_numbers_are_exact_bounded_integers(candidate_index):
    with pytest.raises(ValidationError):
        OriginReply(action="select", candidate_index=candidate_index)


def test_interpretation_verifies_user_quotes_and_current_origin_sources():
    body = chat_request(recent_messages=[{"role": "user", "content": "인천에서 출발할 거야"}])
    model = AsyncMock()
    model.complete.return_value = Interpretation(
        intent="recommendation",
        origin_name="시청역",
        origin_hints=[hint(), hint("recent_user_message"), hint("current_origin", "서울")],
    )
    parsed = asyncio.run(interpret(model, body))
    assert len(parsed.origin_hints) == 3
    assert parsed.patch().model_dump(exclude_unset=True) == {}


@pytest.mark.parametrize(
    "fault", ["invented_quote", "assistant_quote", "missing_message", "no_origin"]
)
def test_interpretation_rejects_invented_or_misattributed_origin_evidence(fault):
    body = chat_request(recent_messages=[{"role": "assistant", "content": "인천에서 출발?"}])
    clue = hint()
    if fault == "invented_quote":
        clue = hint(text="부산")
    elif fault == "assistant_quote":
        clue = hint("recent_user_message")
    elif fault == "missing_message":
        clue = hint("recent_user_message", message_index=4)
    else:
        body.current_conditions.origin = None
        clue = hint("current_origin", "서울")
    model = AsyncMock()
    model.complete.return_value = Interpretation(intent="recommendation", origin_hints=[clue])
    with pytest.raises(ServiceError) as exc:
        asyncio.run(interpret(model, body))
    assert exc.value.code == "AI_INTERPRETATION_FAILED"


def test_model_receives_only_verified_origin_display_context_and_no_selection_token():
    pending = origin_clarification("choose", 2)
    body = chat_request(message="2번이고 20분 이내", pending_clarification=pending)
    display = OriginInterpretationContext(
        mode="choose",
        candidates=[
            {"name": "시청역", "address": "서울 중구"},
            {"name": "동명역", "address": "인천"},
        ],
    )
    model = AsyncMock()
    model.complete.return_value = Interpretation(
        intent="recommendation",
        origin_reply={"action": "select", "candidate_index": 2},
        max_travel_seconds=1200,
    )
    parsed = asyncio.run(interpret(model, body, origin_confirmation=display))
    assert parsed.origin_reply.candidate_index == 2
    assert parsed.patch().model_dump(exclude_unset=True) == {"max_travel_seconds": 1200}
    _, payload, _ = model.complete.call_args.args
    assert ORIGIN_TOKEN not in repr(payload) and "origin-1" not in repr(payload)
    assert payload["origin_confirmation"] == display.model_dump(mode="json")
    assert "choices" not in payload["pending_clarification"]


@pytest.mark.parametrize("fault", ["unverified", "ambiguous_yes", "out_of_range"])
def test_selection_needs_verified_context_and_an_unambiguous_existing_candidate(fault):
    body = chat_request(pending_clarification=origin_clarification("choose", 2))
    display = OriginInterpretationContext(
        mode="choose", candidates=[{"name": "역", "address": "서울"}] * 2
    )
    reply = {"action": "select", "candidate_index": 2}
    if fault == "unverified":
        display = None
    elif fault == "ambiguous_yes":
        reply = {"action": "confirm"}
    else:
        reply["candidate_index"] = 3
    model = AsyncMock()
    model.complete.return_value = Interpretation(intent="recommendation", origin_reply=reply)
    with pytest.raises(ServiceError) as exc:
        asyncio.run(interpret(model, body, origin_confirmation=display))
    assert exc.value.code == "AI_INTERPRETATION_FAILED"


@pytest.mark.parametrize("field", ["coordinates", "place_id", "candidates", "ranking"])
def test_interpretation_cannot_generate_provider_facts(field):
    with pytest.raises(ValidationError):
        Interpretation.model_validate({"intent": "recommendation", field: "invented"})


def test_confirmed_origin_is_preserved_while_other_conditions_are_pending():
    wire = origin_clarification("refine", 0)
    wire["origin_context"]["mode"] = "confirmed"
    wire["required_fields"] = ["purposes"]
    wire["pending_patch"]["origin"] = origin_choice()["value"]["location"]
    value = Clarification.model_validate(wire)
    assert value.model_dump(mode="json") == wire
    for bad in (
        wire | {"required_fields": ["origin"]},
        wire | {"pending_patch": {}},
        wire | {"choices": [origin_choice()]},
    ):
        with pytest.raises(ValidationError):
            Clarification.model_validate(bad)


@pytest.mark.parametrize(
    "reply", [{"action": "confirm"}, {"action": "select", "candidate_index": 1}]
)
def test_model_cannot_reselect_an_already_confirmed_origin(reply):
    display = OriginInterpretationContext(
        mode="confirmed", candidates=[{"name": "시청역", "address": "서울 중구"}]
    )
    model = AsyncMock()
    model.complete.return_value = Interpretation(intent="recommendation", origin_reply=reply)
    with pytest.raises(ServiceError) as exc:
        asyncio.run(interpret(model, chat_request(), origin_confirmation=display))
    assert exc.value.code == "AI_INTERPRETATION_FAILED"
