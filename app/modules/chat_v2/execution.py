"""Internal F02 entry point: authenticate/admit -> interpret/prepare -> commit.

DialogueExecution is also used by the P3 leased runtime and opt-in local HTTP app.
"""

from contextlib import asynccontextmanager

from app.modules.chat_v2.finalization import DatabaseFinalizer
from app.modules.chat_v2.recommendation_factory import open_recommendation_preparer
from app.modules.chat_v2.storage_contracts import ExecutionView
from app.modules.chat_v2.workflow import HeadcountChangeWorkflow
from app.modules.recommendation.progress import RecommendationWork, SearchProgress


class StoredHeadcountExecution:
    def __init__(self, store, interpreter, prepare_factory):
        self.store, self.interpreter, self.prepare_factory = store, interpreter, prepare_factory

    async def execute(self, credential, conversation_id, request):
        admitted = await self.store.admit(credential, conversation_id, request)
        if isinstance(admitted, ExecutionView):
            return admitted
        claim = admitted
        work = RecommendationWork(
            version=claim.context.condition_version,
            conditions=claim.context.current_conditions.model_copy(deep=True),
            excluded=set(claim.excluded),
            search=SearchProgress(),
        )
        finalizer = DatabaseFinalizer(self.store, claim)
        async with self.prepare_factory(identity=claim.context.identity, work=work) as preparer:
            response = await HeadcountChangeWorkflow(
                interpreter=self.interpreter, preparer=preparer, finalizer=finalizer
            ).execute(request, claim.context)
        if response.snapshot is None:
            return await self.store.finish_early(claim, response)
        if response.status == "reconciling":
            return ExecutionView(
                identity=claim.context.identity,
                state_version=claim.context.state_version,
                status="reconciling",
                response=response,
            )
        # The response stored with the receipt is authoritative, including replay.
        return await self.store.read_claim(claim)


def build_stored_headcount_execution(
    store, interpreter, readers, http, settings, *, driving_requests
):
    """Interpreter may be open_bedrock_interpreter; all other adapters are real."""

    @asynccontextmanager
    async def prepare_factory(**kwargs):
        async with open_recommendation_preparer(
            readers, http, settings, driving_requests=driving_requests, **kwargs
        ) as preparer:
            yield preparer

    return StoredHeadcountExecution(store, interpreter, prepare_factory)


def build_dialogue_execution(store, interpreter, readers, http, settings, *, driving_requests):
    from app.modules.chat_v2.dialogue import DialogueExecution
    from app.modules.chat_v2.tools import DialogueTools

    @asynccontextmanager
    async def prepare_factory(**kwargs):
        async with open_recommendation_preparer(
            readers, http, settings, driving_requests=driving_requests, **kwargs
        ) as preparer:
            yield preparer

    return DialogueExecution(
        store,
        interpreter,
        DialogueTools(readers, http, settings, driving_requests=driving_requests),
        prepare_factory,
    )
