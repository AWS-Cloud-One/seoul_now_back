"""Replaceable component interfaces for the executable first-slice workflow.

Interpreter -> PlanValidator -> RecommendationPreparer -> Finalizer -> ResponseComposer

The workflow supplies validation and default responses. Interpreter, preparation
and persistence adapters are explicitly injected; there are no fake defaults.
"""

from typing import Protocol

from app.modules.chat_v2.contracts import (
    ConversationContext,
    InterpretationProposal,
    NeedsInputIntent,
    RunRequest,
    UnsupportedIntent,
    ValidatedPlan,
)
from app.modules.chat_v2.responses import ChatResponse
from app.modules.chat_v2.results import (
    FinalizationResult,
    PreparedRecommendation,
    StageError,
)


class Interpreter(Protocol):
    async def interpret(
        self, request: RunRequest, context: ConversationContext
    ) -> InterpretationProposal:
        """Interpret text and focus; never claim ownership, approval or persistence."""
        ...


class PlanValidator(Protocol):
    async def validate(
        self,
        request: RunRequest,
        context: ConversationContext,
        proposal: InterpretationProposal,
    ) -> ValidatedPlan | NeedsInputIntent | UnsupportedIntent:
        """Verify identity, versions, evidence, scope and prerequisites before approval.

        Return questions/notices without approving a plan. Check a server-supplied
        context; the caller owns authentication and loading the current DB state.
        No default or fabricated plan may be returned on a validation failure.
        """
        ...


class RecommendationPreparer(Protocol):
    async def prepare(self, plan: ValidatedPlan) -> PreparedRecommendation:
        """Compute using the existing execution owner, without final persistence."""
        ...


class Finalizer(Protocol):
    async def finalize(self, prepared: PreparedRecommendation) -> FinalizationResult:
        """Recheck versions/cancellation and report actual DB confirmation or uncertainty."""
        ...


class ResponseComposer(Protocol):
    def compose(self, result: FinalizationResult) -> ChatResponse:
        """Expose actual commit/preparation status; never turn preparation into success."""
        ...

    def needs_input(
        self, context: ConversationContext, question: NeedsInputIntent
    ) -> ChatResponse: ...

    def unchanged(self, plan: ValidatedPlan) -> ChatResponse: ...

    def unsupported(self, context: ConversationContext, reason: str) -> ChatResponse: ...

    def failed(self, context: ConversationContext, error: StageError) -> ChatResponse: ...
