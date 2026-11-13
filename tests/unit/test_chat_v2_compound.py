"""Bounded condition algebra and provider-facing structured compound contracts."""

import asyncio

import pytest
from pydantic import ValidationError

from app.modules.chat_v2.contracts import DialogueProposal
from app.modules.chat_v2.dialogue_interpretation import LangChainDialogueInterpreter
from app.modules.chat_v2.predicates import Predicate, combine, outcome_fact
from tests.fixtures.chat_v2 import scenario
from tests.unit.test_chat_v2_interpretation import MODEL, response
from tests.unit.test_chat_v2_interpretation import transport as transport


@pytest.mark.parametrize(
    "operator,values,expected",
    [
        ("all", ["false", "unknown"], "false"),
        ("all", ["true", "unknown"], "unknown"),
        ("any", ["true", "unknown"], "true"),
        ("any", ["false", "unknown"], "unknown"),
    ],
)
def test_three_valued_conditions(operator, values, expected):
    assert combine(operator, values) == expected


def test_unknown_and_failure_are_not_no_matches_and_domains_are_bounded():
    predicate = Predicate(terms=[{"kind": "candidate_outcome", "outcome": "no_matches"}])
    atom = predicate.terms[0]
    assert outcome_fact(atom, "insufficient_evidence") == "unknown"
    assert outcome_fact(atom, "error") == "unknown"
    assert outcome_fact(atom, "no_candidates") == "true"
    with pytest.raises(ValueError):
        predicate.validate_domain("candidate")
    with pytest.raises(ValidationError):
        Predicate(terms=[{"kind": "compare", "metric": "population", "operator": "lt", "value": 3}])
    with pytest.raises(ValidationError):
        Predicate(terms=[{"kind": "registered_parking_exists", "radius_m": 500.5}])


@pytest.mark.parametrize(
    "decision",
    [
        {
            "kind": "recommend",
            "scope": "request_only",
            "max_travel_seconds": 1200,
            "evidence_text": "이번 추천",
        },
        {"kind": "recommend", "use_previous": True, "evidence_text": "이번 추천"},
        {
            "kind": "recommend",
            "constraints": {"terms": [{"kind": "registered_parking_exists", "radius_m": 500}]},
            "alternative": {
                "when": {"terms": [{"kind": "candidate_outcome", "outcome": "no_matches"}]},
                "omit_parking": True,
                "evidence_text": "이번 추천",
            },
            "after": {"position": 1, "topics": ["parking"]},
            "evidence_text": "이번 추천",
        },
        {
            "kind": "compare",
            "metric": "duration_seconds",
            "targets": [{"kind": "position", "position": 1}, {"kind": "position", "position": 2}],
            "evidence_text": "이번 추천",
        },
    ],
)
def test_compound_schema_uses_existing_langchain_transport(transport, decision):
    request, _, prepared, _ = scenario()
    request.input.text = "이번 추천"
    expected = DialogueProposal(decision=decision)
    raw = response(expected.model_dump())
    raw["output"]["message"]["content"][0]["toolUse"]["name"] = "DialogueProposal"
    transport.stubber.add_response("converse", raw)

    async def run():
        interpreter = LangChainDialogueInterpreter(lambda: transport.model, model_id=MODEL)
        try:
            return await interpreter.interpret(request, prepared.plan.context)
        finally:
            await interpreter.aclose()

    assert asyncio.run(run()) == expected
