import asyncio
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.modules.ai.interpretation import HeadcountHint, Interpretation, interpret
from app.schemas.pending import ChatRequest
from tests.unit.recommendation.test_progress import request


@pytest.mark.parametrize(
    "minimum,quote", [(7, "6명 이상"), (6, "-6명 이상"), (5, "6.5명 이상"), (6, "많은 사람")]
)
def test_baseline_must_equal_an_explicit_literal_minimum(minimum, quote):
    with pytest.raises(ValidationError):
        HeadcountHint(minimum=minimum, quote=quote)


def test_retracting_a_range_keeps_the_quote_without_inventing_another_number():
    hint = HeadcountHint(minimum=None, quote="인원이 달라져서 아직 몰라")
    assert hint.minimum is None


def test_echo_of_a_verified_stored_hint_is_not_a_new_change():
    hint = {"minimum": 6, "quote": "6명 이상"}
    model = AsyncMock()
    model.complete.return_value = Interpretation(
        intent="recommendation", headcount_hint=hint, max_travel_seconds=1200
    )
    body = ChatRequest(
        **request().model_dump(exclude={"conditions_patch"}), message="20분 이내로 해줘"
    )
    result = asyncio.run(interpret(model, body, condition_dialogue={"headcount_hint": hint}))
    assert result.headcount_hint is None and result.max_travel_seconds == 1200


def test_proceed_does_not_reimpose_a_model_echo_of_the_stored_prerequisite():
    model = AsyncMock()
    model.complete.return_value = Interpretation(
        intent="recommendation",
        dialogue_action="proceed",
        action_quote="보장 안 돼도 진행해",
        request_relation="conditional",
        relation_quote="주차 가능하면",
    )
    body = ChatRequest(
        **request().model_dump(exclude={"conditions_patch"}), message="보장 안 돼도 진행해"
    )
    result = asyncio.run(
        interpret(
            model,
            body,
            task_dialogue={
                "recommendation_waiting_for": "conditional",
                "condition_quote": "주차 가능하면",
            },
        )
    )
    assert result.request_relation == "independent" and result.relation_quote is None
    assert result.dialogue_action == "proceed"
