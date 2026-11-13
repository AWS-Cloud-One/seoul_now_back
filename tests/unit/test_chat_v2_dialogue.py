"""P2 structured tool parsing and server reference ambiguity (no external calls)."""

import asyncio
import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.modules.chat_v2.contracts import DialogueProposal, TargetSelector
from app.modules.chat_v2.dialogue_context import OriginOption, RecommendationDraft
from app.modules.chat_v2.dialogue_interpretation import (
    LangChainDialogueInterpreter,
    build_dialogue_messages,
)
from app.modules.chat_v2.references import recommendation_references, resolve_targets
from app.modules.chat_v2.responses import DefaultResponseComposer
from app.modules.chat_v2.results import FinalizationResult
from tests.fixtures.chat_v2 import scenario
from tests.unit.test_chat_v2_interpretation import MODEL, response
from tests.unit.test_chat_v2_interpretation import transport as transport


@pytest.mark.parametrize(
    "decision",
    [
        {
            "kind": "recommend",
            "origin_query": "서울역",
            "purposes": [{"purpose": "cafe"}],
            "max_travel_seconds": 1800,
            "evidence_text": "서울역에서 30분 카페",
        },
        {"kind": "select_origin", "position": 1, "for_lookup": True, "evidence_text": "맞아"},
        {
            "kind": "recommend",
            "search_area_query": "성수",
            "origin_query": "수원역",
            "purposes": [{"purpose": "cafe"}],
            "evidence_text": "수원역에서 출발해서 성수 카페",
        },
        {
            "kind": "read",
            "topics": ["parking", "driving"],
            "targets": [{"kind": "position", "position": 2}],
            "evidence_text": "두 번째",
        },
        {"kind": "search_places", "query": "성수 카페", "evidence_text": "성수 카페"},
    ],
)
def test_dialogue_schema_passes_real_langchain_transport(transport, decision):
    request, _, prepared, _ = scenario()
    request.input.text = decision["evidence_text"]
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
    assert transport.calls[0]["toolConfig"]["toolChoice"] == {"tool": {"name": "DialogueProposal"}}


def test_model_context_excludes_coordinates_tokens_and_unrelated_execution_ids():
    request, _, prepared, _ = scenario()
    context = prepared.plan.context
    context.draft = RecommendationDraft(
        base_condition_version=context.condition_version,
        conditions=context.current_conditions,
        origin_options=[
            OriginOption(
                option_id="secret-option-id", position=1, location=context.current_conditions.origin
            )
        ],
    )
    messages = build_dialogue_messages(request, context)
    data = json.loads(messages[1].content.split("\n", 1)[1])
    assert data["draft"]["origin_options"][0]["label"] == "서울역"
    encoded = json.dumps(data)
    for value in ("latitude", "longitude", "token", "trip_id", "session_id", "secret-option-id"):
        assert value not in encoded


def test_positions_never_cross_collections_and_targets_are_strict():
    _, _, prepared, receipt = scenario()
    stored = DefaultResponseComposer().compose(
        FinalizationResult(prepared=prepared, commit={"state": "committed", "receipt": receipt})
    )
    refs = recommendation_references(stored)
    context = prepared.plan.context
    another = refs[0].model_copy(update={"collection_id": uuid4(), "ref_id": "another"})
    context.references = [*refs, another]
    selector = TargetSelector(kind="position", position=1)
    assert resolve_targets([selector], context, None) is None
    viewed = request_view(result_id=receipt.result_id)
    assert resolve_targets([selector], context, viewed) == refs
    with pytest.raises(ValidationError):
        TargetSelector(kind="focus", position=1)
    with pytest.raises(ValidationError):
        DialogueProposal(
            decision={
                "kind": "recommend",
                "origin_query": "서울역",
                "coordinates": [1, 2],
                "evidence_text": "서울역",
            }
        )


def request_view(**values):
    from app.modules.chat_v2.contracts import ViewedContext

    return ViewedContext(**values)
