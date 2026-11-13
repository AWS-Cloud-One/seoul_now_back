"""LangChain interpretation with explicit schema validation and bounded worker calls."""

import asyncio
import json
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from threading import Lock
from time import monotonic

from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectTimeoutError,
    CredentialRetrievalError,
    NoCredentialsError,
    PartialCredentialsError,
    ReadTimeoutError,
    TokenRetrievalError,
)
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
from langsmith import tracing_context
from pydantic import ValidationError

from app.modules.chat_v2.contracts import ConversationContext, InterpretationProposal, RunRequest
from app.modules.chat_v2.model_input import PROMPT_VERSION, build_messages
from app.modules.chat_v2.results import StageError
from app.modules.chat_v2.validation import StageFailure, check_request_context

logger = logging.getLogger(__name__)


class InterpretationFailure(StageFailure):
    def __init__(self, reason: str, message: str, *, retryable: bool = False):
        self.reason = reason
        super().__init__(
            StageError(code="INTERPRETATION_FAILED", safe_message=message, retryable=retryable)
        )


@dataclass(frozen=True)
class InterpretationTrace:
    run_id: str
    request_id: str
    model_id: str
    prompt_version: str
    attempt: int
    outcome: str
    elapsed_ms: int
    input_tokens: int | None
    output_tokens: int | None
    provider_request_id: str | None


class LangChainHeadcountInterpreter:
    """One owned instance per application lifespan; no default or fake model.

    A timed-out/cancelled await does not stop remote inference. Its worker keeps a
    capacity slot until it finishes, and its late result cannot advance the workflow.
    Close this instance before closing its model's clients.
    """

    proposal_type = InterpretationProposal
    message_builder = staticmethod(build_messages)
    prompt_version = PROMPT_VERSION
    input_limit_bytes = 16_000
    validate_versions = True

    def __init__(
        self,
        model_factory: Callable[[], BaseChatModel],
        *,
        model_id: str,
        timeout_seconds: float = 20,
        observer: Callable[[InterpretationTrace], None] | None = None,
    ):
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._model_factory = model_factory
        self.model_id = model_id
        self.timeout_seconds = timeout_seconds
        self._observer = observer
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="chat-v2-model")
        self._slots = asyncio.Semaphore(2)
        self._lock = Lock()
        self._runnable = None
        self._closed = False
        self._schema_bytes = len(
            json.dumps(convert_to_openai_tool(self.proposal_type), ensure_ascii=False).encode()
        )

    def _get_runnable(self):
        with self._lock:
            if self._runnable is None:
                self._runnable = self._model_factory().with_structured_output(
                    self.proposal_type, method="function_calling", include_raw=True
                )
            return self._runnable

    async def interpret(
        self, request: RunRequest, context: ConversationContext
    ) -> InterpretationProposal:
        if self.validate_versions:
            check_request_context(request, context)
        elif request.request_id != context.identity.request_id:
            raise ValueError("Request identity mismatch")
        if self._closed:
            raise RuntimeError("Interpreter is closed")
        messages = self.message_builder(request, context)
        # A conservative byte limit, not a claim about the model's exact tokenizer.
        if (
            sum(len(m.content.encode()) for m in messages) + self._schema_bytes
            > self.input_limit_bytes
        ):
            raise InterpretationFailure("input_limit", "요청 문맥이 너무 길어 해석하지 못했어요.")
        deadline = monotonic() + self.timeout_seconds
        try:
            async with asyncio.timeout(self.timeout_seconds):
                await self._slots.acquire()
                try:
                    future = asyncio.get_running_loop().run_in_executor(
                        self._pool, self._interpret_sync, context.identity, messages, deadline
                    )
                except BaseException:
                    self._slots.release()
                    raise
                future.add_done_callback(self._worker_finished)
                return await asyncio.shield(future)
        except TimeoutError:
            raise InterpretationFailure(
                "timeout", "요청 해석 시간이 초과됐어요. 조건은 변경하지 않았어요.", retryable=True
            ) from None

    def _worker_finished(self, future):
        self._slots.release()
        # Consume exceptions even if the caller already timed out or was cancelled.
        if not future.cancelled():
            future.exception()

    def _interpret_sync(self, identity, messages, deadline):
        # Never export user input to LangSmith through ambient tracing settings.
        with tracing_context(enabled=False, parent=False):
            for attempt in (1, 2):
                if monotonic() >= deadline:
                    raise InterpretationFailure(
                        "timeout", "요청 해석 시간이 초과됐어요.", retryable=True
                    )
                started = monotonic()
                raw = None
                outcome = "provider_error"
                try:
                    output = self._get_runnable().invoke(messages, config={"callbacks": []})
                    raw = output.get("raw")
                    proposal = self._parse(output)
                    outcome = "ok"
                    return proposal
                except InterpretationFailure as exc:
                    outcome = exc.reason
                    if exc.reason != "malformed_output" or attempt == 2:
                        raise
                    messages = [
                        *messages,
                        HumanMessage(
                            content="형식 수정: 같은 원문에 대해 유효한 "
                            f"{self.proposal_type.__name__} 도구 "
                            "출력 하나만 반환하세요. 의미가 불분명하면 needs_input을 사용하세요."
                        ),
                    ]
                except ConnectTimeoutError, ReadTimeoutError:
                    outcome = "timeout"
                    raise InterpretationFailure(
                        outcome, "모델 응답 시간이 초과됐어요.", retryable=True
                    ) from None
                except (
                    NoCredentialsError,
                    PartialCredentialsError,
                    CredentialRetrievalError,
                    TokenRetrievalError,
                ):
                    outcome = "authentication"
                    raise InterpretationFailure(
                        outcome, "모델 연결 인증을 확인해야 해요. 조건은 변경하지 않았어요."
                    ) from None
                except ClientError as exc:
                    # Provider transport boundary: never return/log raw AWS messages.
                    code = exc.response.get("Error", {}).get("Code")
                    retryable = code in {
                        "ThrottlingException",
                        "ModelTimeoutException",
                        "ServiceUnavailableException",
                        "InternalServerException",
                    }
                    outcome = "rate_limited" if code == "ThrottlingException" else "provider_error"
                    raise InterpretationFailure(
                        outcome,
                        "모델 제공처 요청에 실패했어요. 조건은 변경하지 않았어요.",
                        retryable=retryable,
                    ) from None
                except BotoCoreError:
                    outcome = "provider_error"
                    raise InterpretationFailure(
                        outcome, "모델 연결에 실패했어요. 조건은 변경하지 않았어요.", retryable=True
                    ) from None
                finally:
                    self._record(identity, raw, attempt, outcome, monotonic() - started)
        raise AssertionError("Unreachable")

    def _parse(self, output):
        raw = output.get("raw")
        if not isinstance(raw, AIMessage):
            raise InterpretationFailure("malformed_output", "모델 응답 형식을 확인하지 못했어요.")
        stop = raw.response_metadata.get("stopReason")
        if stop in {"guardrail_intervened", "content_filtered", "refusal"}:
            raise InterpretationFailure("refusal", "모델이 이 요청의 해석을 제공하지 않았어요.")
        if stop == "max_tokens":
            raise InterpretationFailure(
                "output_limit", "모델 응답이 한도를 넘어 해석을 마치지 못했어요."
            )
        calls = raw.tool_calls
        if (
            stop != "tool_use"
            or raw.invalid_tool_calls
            or len(calls) != 1
            or calls[0]["name"] != self.proposal_type.__name__
            or output.get("parsing_error") is not None
        ):
            raise InterpretationFailure("malformed_output", "모델 응답 형식을 확인하지 못했어요.")
        try:
            # Revalidate the raw arguments as well: do not accept parser coercion or repair.
            proposal = self.proposal_type.model_validate(calls[0]["args"])
            if output.get("parsed") != proposal:
                raise ValueError("Parsed and raw proposal disagree")
            return proposal
        except ValidationError, ValueError, TypeError:
            raise InterpretationFailure(
                "malformed_output", "모델의 변경안을 확인하지 못했어요."
            ) from None

    def _record(self, identity, raw, attempt, outcome, elapsed):
        usage = raw.usage_metadata or {} if isinstance(raw, AIMessage) else {}
        metadata = raw.response_metadata if isinstance(raw, AIMessage) else {}
        trace = InterpretationTrace(
            run_id=str(identity.run_id),
            request_id=str(identity.request_id),
            model_id=self.model_id,
            prompt_version=self.prompt_version,
            attempt=attempt,
            outcome=outcome,
            elapsed_ms=round(elapsed * 1000),
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            provider_request_id=metadata.get("ResponseMetadata", {}).get("RequestId"),
        )
        logger.info("chat_v2_interpretation", extra={"interpretation": asdict(trace)})
        if self._observer is not None:
            try:
                self._observer(trace)
            except Exception:
                logger.warning("chat_v2_interpretation_observer_failed")

    async def aclose(self):
        self._closed = True
        await asyncio.to_thread(self._pool.shutdown, wait=True, cancel_futures=True)
