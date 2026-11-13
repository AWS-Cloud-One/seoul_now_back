"""Real LangChain + Bedrock parsing, with SDK responses stubbed (no model/network)."""

import asyncio
import json
from contextlib import ExitStack, closing
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import boto3
import pytest
from botocore.stub import Stubber
from langchain_aws import ChatBedrockConverse

from app.modules.chat_v2.contracts import InterpretationProposal
from app.modules.chat_v2.interpretation import InterpretationFailure, LangChainHeadcountInterpreter
from app.modules.chat_v2.model_input import build_messages
from app.modules.chat_v2.validation import HeadcountPlanValidator, StageFailure
from app.modules.chat_v2.workflow import HeadcountChangeWorkflow
from tests.fixtures.chat_v2 import scenario

MODEL = "global.anthropic.claude-haiku-4-5-20251001-v1:0"


def response(proposal, *, stop="tool_use"):
    return {
        "output": {
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "toolUse": {
                            "toolUseId": "test-tool",
                            "name": "InterpretationProposal",
                            "input": proposal,
                        }
                    }
                ],
            }
        },
        "stopReason": stop,
        "usage": {"inputTokens": 10, "outputTokens": 20, "totalTokens": 30},
        "metrics": {"latencyMs": 1},
        "ResponseMetadata": {"RequestId": "synthetic-provider-request"},
    }


@pytest.fixture
def transport(monkeypatch):
    with ExitStack() as stack:
        session = boto3.Session(
            aws_access_key_id="testing",
            aws_secret_access_key="testing",
            region_name="ap-northeast-2",
        )
        runtime = stack.enter_context(closing(session.client("bedrock-runtime")))
        control = stack.enter_context(closing(session.client("bedrock")))
        stubber = stack.enter_context(Stubber(runtime))
        calls = []
        original = runtime.converse

        def capture(**kwargs):
            calls.append(kwargs)
            return original(**kwargs)

        monkeypatch.setattr(runtime, "converse", capture)
        model = ChatBedrockConverse(
            model_id=MODEL,
            client=runtime,
            bedrock_client=control,
            max_tokens=512,
            temperature=0,
            disable_streaming=True,
            cache=False,
        )
        yield SimpleNamespace(model=model, stubber=stubber, calls=calls)
        stubber.assert_no_pending_responses()


async def interpret_once(transport, request, context, traces):
    interpreter = LangChainHeadcountInterpreter(
        lambda: transport.model, model_id=MODEL, observer=traces.append
    )
    try:
        return await interpreter.interpret(request, context)
    finally:
        await interpreter.aclose()


@pytest.mark.parametrize(
    "decision",
    [
        {
            "kind": "change_headcount",
            "headcount": 3,
            "scope": "persistent",
            "evidence_text": "셋이요",
        },
        {"kind": "needs_input", "required_field": "headcount", "question": "몇 명인가요?"},
        {"kind": "unsupported", "reason": "가정 질문은 인원을 변경하지 않아요."},
    ],
)
def test_structured_union_and_usage_pass_through_real_langchain(transport, decision):
    request, _, prepared, _ = scenario()
    context = prepared.plan.context
    expected = InterpretationProposal(decision=decision)
    transport.stubber.add_response("converse", response(expected.model_dump()))
    traces = []
    actual = asyncio.run(interpret_once(transport, request, context, traces))
    assert actual == expected
    trace = traces[0]
    assert trace.run_id == str(context.identity.run_id)
    assert trace.request_id == str(request.request_id)
    assert (trace.input_tokens, trace.output_tokens, trace.attempt) == (10, 20, 1)
    assert trace.outcome == "ok"
    wire = transport.calls[0]
    assert wire["toolConfig"]["toolChoice"] == {"tool": {"name": "InterpretationProposal"}}
    assert wire["inferenceConfig"]["maxTokens"] == 512


def test_input_contains_question_and_current_count_without_ids_or_coordinates():
    request, _, prepared, _ = scenario()
    context = prepared.plan.context
    messages = build_messages(request, context)
    assert messages[-1].content == "셋이요"
    payload = json.loads(messages[1].content.split("\n", 1)[1])
    assert payload["current_conditions"]["headcount"] == 2
    assert payload["pending_question"]["scope"] == "persistent"
    encoded = json.dumps(payload)
    for excluded in ("latitude", "longitude", "token", "request_id", "trip_id", "clarification_id"):
        assert excluded not in encoded


def test_invalid_output_has_only_one_repair_and_all_usage_is_recorded(transport):
    request, expected, prepared, _ = scenario()
    invalid = expected.model_dump()
    invalid["decision"]["headcount"] = "3"  # Must not silently coerce a model string.
    transport.stubber.add_response("converse", response(invalid))
    transport.stubber.add_response("converse", response(expected.model_dump()))
    traces = []
    actual = asyncio.run(interpret_once(transport, request, prepared.plan.context, traces))
    assert actual == expected
    assert [t.outcome for t in traces] == ["malformed_output", "ok"]
    assert sum(t.input_tokens for t in traces) == 20
    assert len(transport.calls) == 2


def test_two_invalid_outputs_fail_without_clarification_or_downstream_calls(transport):
    request, proposal, prepared, _ = scenario()
    invalid = proposal.model_dump()
    invalid["decision"]["headcount"] = -1
    for _ in range(2):
        transport.stubber.add_response("converse", response(invalid))
    preparer, finalizer = (
        SimpleNamespace(prepare=AsyncMock()),
        SimpleNamespace(finalize=AsyncMock()),
    )

    async def run():
        interpreter = LangChainHeadcountInterpreter(lambda: transport.model, model_id=MODEL)
        try:
            return await HeadcountChangeWorkflow(
                interpreter=interpreter, preparer=preparer, finalizer=finalizer
            ).execute(request, prepared.plan.context)
        finally:
            await interpreter.aclose()

    result = asyncio.run(run())
    assert result.kind == "failure" and result.clarification is None
    assert result.error.code == "INTERPRETATION_FAILED" and not result.error.retryable
    preparer.prepare.assert_not_awaited()
    finalizer.finalize.assert_not_awaited()


@pytest.mark.parametrize(
    "code,retryable", [("AccessDeniedException", False), ("ThrottlingException", True)]
)
def test_provider_failure_is_sanitized_and_never_repaired(transport, code, retryable):
    request, _, prepared, _ = scenario()
    transport.stubber.add_client_error("converse", code, "private provider details")
    with pytest.raises(InterpretationFailure) as caught:
        asyncio.run(interpret_once(transport, request, prepared.plan.context, []))
    assert caught.value.error.retryable is retryable
    assert "private" not in caught.value.error.safe_message
    assert len(transport.calls) == 1


def test_refusal_does_not_become_a_user_clarification(transport):
    request, proposal, prepared, _ = scenario()
    transport.stubber.add_response(
        "converse", response(proposal.model_dump(), stop="guardrail_intervened")
    )
    with pytest.raises(InterpretationFailure, match="INTERPRETATION_FAILED") as caught:
        asyncio.run(interpret_once(transport, request, prepared.plan.context, []))
    assert caught.value.reason == "refusal"
    assert len(transport.calls) == 1


def test_valid_model_schema_still_requires_server_evidence_check(transport):
    request, proposal, prepared, _ = scenario()
    proposal.decision.evidence_text = "원문에 없는 근거"
    transport.stubber.add_response("converse", response(proposal.model_dump()))

    async def run():
        decoded = await interpret_once(transport, request, prepared.plan.context, [])
        return await HeadcountPlanValidator().validate(request, prepared.plan.context, decoded)

    with pytest.raises(StageFailure) as caught:
        asyncio.run(run())
    assert caught.value.error.code == "VALIDATION_FAILED"


def test_timeout_keeps_worker_capacity_until_late_result_finishes():
    request, _, prepared, _ = scenario()
    started, finish = Event(), Event()

    def blocked(*args, **kwargs):
        started.set()
        assert finish.wait(2)
        raise RuntimeError("late private response")

    model = Mock()
    model.with_structured_output.return_value.invoke.side_effect = blocked

    async def run():
        interpreter = LangChainHeadcountInterpreter(
            lambda: model, model_id=MODEL, timeout_seconds=0.03
        )
        try:
            with pytest.raises(InterpretationFailure) as caught:
                await interpreter.interpret(request, prepared.plan.context)
            assert caught.value.reason == "timeout" and started.is_set()
            assert interpreter._slots._value == 1
        finally:
            finish.set()
            await interpreter.aclose()
        assert interpreter._slots._value == 2

    asyncio.run(run())
