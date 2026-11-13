"""F01/F02/F04/F05/F09 explicit dispatch, with one model interpretation per run."""

from uuid import uuid4

from app.clients.kakao import KakaoError
from app.core.exceptions import ServiceError
from app.modules.chat_v2.comparison import compare_routes, comparison_text
from app.modules.chat_v2.compound import alternative_plan, configure_draft, plan_from_draft
from app.modules.chat_v2.contracts import (
    CompareIntent,
    DestinationSelectionIntent,
    DialogueProposal,
    HeadcountChangeIntent,
    NeedsInputIntent,
    OriginSelectionIntent,
    PendingHeadcountQuestion,
    PlaceSearchIntent,
    ReadIntent,
    RecommendationIntent,
    RecommendationPlan,
    RunRequest,
    UnsupportedIntent,
)
from app.modules.chat_v2.control import checkpoint
from app.modules.chat_v2.dialogue_context import (
    OriginOption,
    PendingRead,
    RecommendationDraft,
)
from app.modules.chat_v2.finalization import DatabaseFinalizer
from app.modules.chat_v2.predicates import EvaluationSummary, evaluate, outcome_fact
from app.modules.chat_v2.references import merge_references, resolve_targets
from app.modules.chat_v2.region_resolution import resolve_region
from app.modules.chat_v2.responses import ChatResponse, DefaultResponseComposer
from app.modules.chat_v2.results import PreparedRecommendation, StageError
from app.modules.chat_v2.storage_contracts import ExecutionView, StoredContext
from app.modules.chat_v2.tools import explain_lookup
from app.modules.chat_v2.validation import (
    StageFailure,
    check_request_context,
)
from app.modules.recommendation.progress import RecommendationWork, SearchProgress
from app.schemas.recommendations import Conditions


class DialogueExecution:
    def __init__(self, store, interpreter, tools, prepare_factory):
        self.store, self.interpreter = store, interpreter
        self.tools, self.prepare_factory = tools, prepare_factory
        self.composer = DefaultResponseComposer()

    async def execute(self, credential, conversation_id, request):
        claim = await self.store.admit(credential, conversation_id, request, dialogue=True)
        if isinstance(claim, ExecutionView):
            return claim
        return await self.execute_claim(claim, request)

    async def execute_claim(self, claim, request, retry_source=None, *, approved_action=None):
        context = claim.context
        state = StoredContext(
            pending_question=context.pending_question,
            search_area=context.search_area,
            draft=context.draft,
            references=merge_references([], context.references),
            focus_ref=context.focus_ref,
            pending_read=context.pending_read,
            excluded=list(claim.excluded),
            active_flow=context.active_flow,
        )
        if retry_source is not None:
            return await self._retry(claim, request, state, retry_source)
        action = approved_action or claim.approved_action
        if action is not None:
            # Only the admission transaction can supply this stored decision.
            proposal = DialogueProposal(decision=action["decision"])
            request = RunRequest.model_validate(
                {
                    **request.model_dump(mode="json"),
                    "input": {
                        "type": "message",
                        "text": action["label"],
                        "reply_to_clarification_id": action["clarification_id"],
                    },
                    "viewed_context": None,
                }
            )
            await checkpoint("applying_action", operation=action)
        else:
            if request.input.type != "message":
                raise ValueError("A choice requires an admitted server decision")
            await checkpoint(
                "interpreting",
                operation={
                    "kind": "interpretation",
                    "input": request.input.model_dump(mode="json"),
                    "viewed_context": request.viewed_context.model_dump(mode="json")
                    if request.viewed_context
                    else None,
                },
            )
            try:
                proposal = DialogueProposal.model_validate(
                    (
                        await self.interpreter.interpret(
                            request.model_copy(deep=True), context.model_copy(deep=True)
                        )
                    ).model_dump()
                )
            except StageFailure as exc:
                return await self._save(claim, self.composer.failed(context, exc.error), state)
            except Exception:
                return await self._fail(
                    claim,
                    state,
                    "INTERPRETATION_FAILED",
                    "요청 해석에 실패했어요. 조건은 변경하지 않았어요.",
                )
        decision = proposal.decision
        if hasattr(decision, "evidence_text") and decision.evidence_text not in request.input.text:
            return await self._fail(
                claim, state, "VALIDATION_FAILED", "요청 해석의 원문 근거를 확인하지 못했어요."
            )
        try:
            alternative = getattr(decision, "alternative", None)
            if alternative and alternative.evidence_text not in request.input.text:
                return await self._fail(
                    claim, state, "VALIDATION_FAILED", "대안 실행의 원문 근거를 확인하지 못했어요."
                )
            if isinstance(decision, UnsupportedIntent):
                return await self._save(
                    claim, self.composer.unsupported(context, decision.reason), state
                )
            if isinstance(decision, PlaceSearchIntent):
                await checkpoint("searching", operation={"kind": "search", "query": decision.query})
                lookup = await self.tools.search_places(decision.query, context.identity.run_id)
                state.references = merge_references(state.references, lookup.places)
                state.focus_ref = lookup.places[0].ref_id if len(lookup.places) == 1 else None
                return await self._answer(claim, state, lookup)
            if isinstance(decision, OriginSelectionIntent) and decision.for_lookup:
                pending = context.pending_read
                if (
                    request.expected_state_version != context.state_version
                    or request.expected_condition_version != context.condition_version
                    or (
                        request.input.reply_to_clarification_id is not None
                        and (
                            pending is None
                            or request.input.reply_to_clarification_id != pending.clarification_id
                        )
                    )
                ):
                    return await self._fail(
                        claim,
                        state,
                        "CONFLICT",
                        "조회 출발지 후보의 현재 상태를 다시 확인해 주세요.",
                    )
                option = (
                    next(
                        (o for o in pending.origin_options if o.position == decision.position), None
                    )
                    if pending
                    else None
                )
                if option is None:
                    return await self._fail(
                        claim, state, "CONFLICT", "확인할 이동 출발지 후보가 없어요."
                    )
                from app.modules.chat_v2.contracts import TargetSelector

                targets = resolve_targets(
                    [TargetSelector(kind="reference", ref_id=ref) for ref in pending.target_refs],
                    context,
                    None,
                )
                if not targets:
                    return await self._fail(
                        claim, state, "CONFLICT", "조회할 장소를 다시 선택해 주세요."
                    )
                lookup_context = context.model_copy(deep=True)
                lookup_context.current_conditions.origin = option.location
                lookup = await self._read(
                    pending.topics,
                    targets,
                    lookup_context,
                    comparison_metric=pending.comparison_metric,
                )
                state.pending_read = None
                comparison = (
                    compare_routes(lookup, pending.comparison_metric, option.location)
                    if pending.comparison_metric
                    else None
                )
                return await self._answer(claim, state, lookup, comparison=comparison)
            comparison_metric = None
            if isinstance(decision, CompareIntent):
                comparison_metric = decision.metric
                decision = ReadIntent(
                    kind="read",
                    targets=decision.targets,
                    topics=["driving", "traffic"] if decision.include_traffic else ["driving"],
                    origin_query=decision.origin_query,
                    evidence_text=decision.evidence_text,
                )
            if isinstance(decision, ReadIntent):
                targets = resolve_targets(decision.targets, context, request.viewed_context)
                if (
                    comparison_metric is None
                    and context.pending_read
                    and "driving" in decision.topics
                    and (
                        not context.pending_read.target_refs
                        or targets is not None
                        and {t.ref_id for t in targets} == set(context.pending_read.target_refs)
                    )
                ):
                    comparison_metric = context.pending_read.comparison_metric
                if targets is None:
                    state.pending_read = PendingRead(
                        clarification_id=str(uuid4()),
                        topics=decision.topics,
                        comparison_metric=comparison_metric,
                        question="어느 장소인지 이름이나 보던 목록의 번호를 알려주세요.",
                    )
                    return await self._question(claim, state, "target", state.pending_read.question)
                if "driving" in decision.topics and (
                    context.current_conditions.origin is None or decision.origin_query
                ):
                    pending = PendingRead(
                        clarification_id=str(uuid4()),
                        topics=decision.topics,
                        comparison_metric=comparison_metric,
                        target_refs=[t.ref_id for t in targets],
                        question="이 장소까지의 이동시간을 확인할 출발지를 알려주세요.",
                    )
                    if decision.origin_query:
                        await checkpoint("searching_origin")
                        found = await self.tools.search_origins(decision.origin_query)
                        pending.origin_options = [
                            OriginOption(option_id=str(uuid4()), position=i, location=c.location)
                            for i, c in enumerate(found.candidates, 1)
                        ]
                        pending.question = (
                            "이동시간 조회에 사용할 출발지를 확인해 주세요."
                            if (pending.origin_options)
                            else "출발지를 찾지 못했어요. 서울·인천·경기의 주소를 더 알려주세요."
                        )
                    state.pending_read = pending
                    return await self._question(
                        claim,
                        state,
                        "lookup_origin",
                        pending.question,
                        options=pending.origin_options,
                    )
                lookup = await self._read(
                    decision.topics, targets, context, comparison_metric=comparison_metric
                )
                state.pending_read = None
                state.focus_ref = targets[0].ref_id if len(targets) == 1 else None
                comparison = (
                    compare_routes(lookup, comparison_metric, context.current_conditions.origin)
                    if comparison_metric
                    else None
                )
                return await self._answer(claim, state, lookup, comparison=comparison)
            # Original UI versions guard all questions/drafts/condition-changing intents.
            check_request_context(request, context)
            if isinstance(decision, NeedsInputIntent):
                state.pending_question = PendingHeadcountQuestion(
                    clarification_id=str(uuid4()),
                    question=decision.question,
                    scope=context.pending_question.scope
                    if context.pending_question
                    else "persistent",
                    field=decision.required_field,
                )
                return await self._question(
                    claim, state, decision.required_field, decision.question
                )
            if (
                isinstance(decision, HeadcountChangeIntent)
                and context.draft is None
                and context.current_conditions.headcount == decision.headcount
                and decision.scope == "persistent"
            ):
                state.pending_question = None
                response = self.composer._early(
                    context,
                    kind="unchanged",
                    status="succeeded",
                    answer=f"현재 인원은 이미 {decision.headcount}명이에요. 조건을 유지했어요.",
                )
                return await self._save(claim, response, state)
            draft = (
                context.draft.model_copy(deep=True)
                if context.draft
                else RecommendationDraft(
                    base_condition_version=context.condition_version,
                    search_area=context.search_area,
                    conditions=context.current_conditions.model_copy(deep=True),
                )
            )
            if draft.base_condition_version != context.condition_version:
                return await self._fail(
                    claim,
                    state,
                    "CONFLICT",
                    "추천 초안의 기준 조건이 변경됐어요. 현재 조건에서 다시 요청해 주세요.",
                )
            configure_draft(draft, decision, context)
            if isinstance(decision, DestinationSelectionIntent):
                if (
                    context.pending_question is None
                    or context.pending_question.field != "search_area"
                    or not draft.destination_options
                ):
                    return await self._fail(
                        claim,
                        state,
                        "CONFLICT",
                        "선택할 지역 후보가 없어요. 찾을 곳을 다시 알려주세요.",
                    )
                option = next(
                    (o for o in draft.destination_options if o.position == decision.position), None
                )
                if option is None:
                    return await self._question(
                        claim, state, "search_area", "제시한 지역 후보 중 하나를 선택해 주세요."
                    )
                draft.search_area = option.region.model_copy(deep=True)
                draft.destination_options = []
                draft.destination_query = None
                draft.destination_radius_m = None
                draft.destination_relation = "area"
            elif isinstance(decision, OriginSelectionIntent):
                if (
                    context.pending_question is None
                    or context.pending_question.field != "origin"
                    or not draft.origin_options
                ):
                    return await self._fail(
                        claim,
                        state,
                        "CONFLICT",
                        "선택할 출발지 후보가 없어요. 출발지를 다시 알려주세요.",
                    )
                option = next(
                    (o for o in draft.origin_options if o.position == decision.position), None
                )
                if option is None:
                    return await self._question(
                        claim, state, "origin", "제시한 출발지 후보 중 하나를 선택해 주세요."
                    )
                draft.conditions.origin = option.location.model_copy(deep=True)
                draft.origin_options = []
            elif isinstance(decision, (RecommendationIntent, HeadcountChangeIntent)):
                values = draft.conditions.model_dump()
                fields = (
                    ("headcount",)
                    if isinstance(decision, HeadcountChangeIntent)
                    else ("purposes", "headcount", "max_travel_seconds")
                )
                for field in fields:
                    value = getattr(decision, field)
                    if value is not None:
                        values[field] = value
                draft.conditions = Conditions.model_validate(values)
                area_query = getattr(decision, "search_area_query", None)
                radius = getattr(decision, "search_radius_m", None)
                relation = getattr(decision, "spatial_relation", None)
                pending_area = (
                    context.pending_question and context.pending_question.field == "search_area"
                )
                if area_query or radius is not None or relation is not None:
                    if pending_area:
                        area_query = area_query or draft.destination_query
                        if radius is None:
                            radius = draft.destination_radius_m
                        if relation is None:
                            relation = draft.destination_relation
                    resolved = await resolve_region(
                        self.tools.readers,
                        self.tools.kakao,
                        area_query,
                        radius=radius,
                        relation=relation or "area",
                        current=draft.search_area,
                    )
                    if resolved.status == "unavailable":
                        return await self._fail(
                            claim, state, "RECOMMENDATION_FAILED", resolved.message
                        )
                    draft.destination_query = area_query
                    draft.destination_radius_m = radius
                    draft.destination_relation = relation or "area"
                    draft.destination_options = resolved.options
                    if resolved.status != "resolved":
                        state.draft = draft
                        state.pending_question = PendingHeadcountQuestion(
                            clarification_id=str(uuid4()),
                            scope=draft.scope,
                            field="search_area",
                            question=resolved.message,
                        )
                        return await self._question(claim, state, "search_area", resolved.message)
                    draft.search_area = resolved.region
                    draft.destination_query = None
                    draft.destination_radius_m = None
                    draft.destination_relation = "area"
                elif pending_area:
                    state.draft = draft
                    return await self._question(
                        claim, state, "search_area", context.pending_question.question
                    )
                query = getattr(decision, "origin_query", None)
                if query:
                    await checkpoint("searching_origin")
                    found = await self.tools.search_origins(query)
                    draft.conditions.origin = None
                    draft.origin_options = [
                        OriginOption(option_id=str(uuid4()), position=i, location=c.location)
                        for i, c in enumerate(found.candidates, 1)
                    ]
                    state.draft = draft
                    question = (
                        "출발지를 확인해 주세요."
                        if draft.origin_options
                        else "조회한 범위에서 지원 출발지를 찾지 못했어요. "
                        "서울·인천·경기 지역이나 주소를 더 알려주세요."
                    )
                    state.pending_question = PendingHeadcountQuestion(
                        clarification_id=str(uuid4()),
                        scope=draft.scope,
                        field="origin",
                        question=question,
                    )
                    return await self._question(
                        claim,
                        state,
                        "origin",
                        question,
                        options=draft.origin_options,
                        warnings=[w.code for w in found.warnings],
                    )
            else:
                return await self._fail(
                    claim, state, "VALIDATION_FAILED", "지원하는 요청 종류를 확인하지 못했어요."
                )
            state.draft = draft
            if draft.origin_options:
                return await self._question(
                    claim,
                    state,
                    "origin",
                    "출발지 후보를 먼저 확인해 주세요.",
                    options=draft.origin_options,
                )
            needs_route = draft.conditions.max_travel_seconds is not None or (
                draft.constraints and any(a.kind == "compare" for a in draft.constraints.atoms())
            )
            needs_origin = needs_route and draft.conditions.origin is None
            if needs_origin or not draft.conditions.purposes:
                field = "origin" if needs_origin else "purposes"
                question = (
                    "자동차 이동시간 조건을 확인하려면 출발지가 필요해요. 어디에서 출발하나요?"
                    if field == "origin"
                    else "카페·식사·산책·전시 중 어떤 곳을 찾나요?"
                )
                state.pending_question = PendingHeadcountQuestion(
                    clarification_id=str(uuid4()),
                    scope=draft.scope,
                    field=field,
                    question=question,
                )
                return await self._question(claim, state, field, question)
            plan = plan_from_draft(draft, context)
            return await self._recommend(claim, plan)
        except StageFailure as exc:
            return await self._save(claim, self.composer.failed(context, exc.error), state)
        except ServiceError, KakaoError, TimeoutError:
            # No fake no-match result when an actual lookup failed.
            return await self._fail(
                claim,
                state,
                "RECOMMENDATION_FAILED",
                "정보 조회에 실패했어요. 기존 조건과 대기 질문을 유지했어요.",
            )

        except ValueError:
            return await self._fail(
                claim, state, "VALIDATION_FAILED", "요청의 조건이나 대상 형식을 검증하지 못했어요."
            )

    async def _recommend(self, claim, plan):
        await checkpoint(
            "recommending",
            operation={"kind": "recommendation", "plan": plan.model_dump(mode="json")},
        )
        work = RecommendationWork(
            version=claim.context.condition_version,
            conditions=claim.context.current_conditions.model_copy(deep=True),
            excluded=set(plan.flow_excluded if plan.scope == "request_only" else claim.excluded),
            search=SearchProgress(),
        )
        try:
            async with self.prepare_factory(identity=claim.context.identity, work=work) as preparer:
                prepared = PreparedRecommendation.model_validate(
                    (await preparer.prepare(plan)).model_dump()
                )
            if prepared.plan != plan:
                raise ValueError("Recommendation changed its approved plan")
            if (
                plan.alternative
                and evaluate(
                    plan.alternative.when, lambda atom: outcome_fact(atom, prepared.outcome)
                )
                == "true"
            ):
                primary = prepared.outcome
                plan = alternative_plan(plan)
                await checkpoint(
                    "recommending",
                    operation={"kind": "recommendation", "plan": plan.model_dump(mode="json")},
                )
                async with self.prepare_factory(
                    identity=claim.context.identity, work=work
                ) as preparer:
                    prepared = PreparedRecommendation.model_validate(
                        (await preparer.prepare(plan)).model_dump()
                    )
                if prepared.plan != plan:
                    raise ValueError("Alternative changed its approved plan")
                prepared.evaluation = prepared.evaluation or EvaluationSummary()
                prepared.evaluation.branch, prepared.evaluation.primary_outcome = (
                    "alternative",
                    primary,
                )
        except (StageFailure, TimeoutError) as exc:
            if isinstance(exc, StageFailure) and exc.error.code != "RECOMMENDATION_FAILED":
                return await self.store.finish_early(
                    claim, self.composer.failed(claim.context, exc.error)
                )
            prepared = PreparedRecommendation(
                plan=plan,
                outcome="error",
                prepared_at=self.store.clock(),
                error=exc.error
                if isinstance(exc, StageFailure)
                else StageError(
                    code="RECOMMENDATION_FAILED",
                    safe_message="추천 조회 시간이 초과됐어요.",
                    retryable=True,
                ),
            )
        except Exception:
            return await self.store.finish_early(
                claim,
                self.composer.failed(
                    claim.context,
                    StageError(
                        code="INTERNAL_ERROR",
                        safe_message="추천 준비 결과를 검증하지 못했어요.",
                        retryable=False,
                    ),
                ),
            )
        if plan.commit_guard:
            guard = evaluate(plan.commit_guard, lambda atom: outcome_fact(atom, prepared.outcome))
            prepared.evaluation = prepared.evaluation or EvaluationSummary()
            prepared.evaluation.guard = guard
            if guard != "true":
                response = self.composer._early(
                    claim.context,
                    kind="decision",
                    status="succeeded" if guard == "false" else "partial",
                    answer="변경 전제가 충족되지 않아 기존 조건을 유지했어요."
                    if guard == "false"
                    else "변경 전제를 확인할 자료가 부족해 기존 조건을 유지했어요.",
                    evaluation=prepared.evaluation,
                )
                return await self.store.finish_early(claim, response)
        result = await DatabaseFinalizer(self.store, claim).finalize(prepared)
        if result.commit.state == "unknown":
            return ExecutionView(
                identity=claim.context.identity,
                state_version=claim.context.state_version,
                status="reconciling",
                response=self.composer.compose(result),
            )
        if result.commit.state == "committed" and plan.after and result.commit.receipt.result_id:
            return await self._continue(claim)
        return await self.store.read_claim(claim)

    async def _continue(self, claim):
        from app.modules.chat_v2.dialogue_context import PlaceReference
        from app.schemas.location import Location

        view, operation = await self.store.continuation_state(claim)
        if view.status == "finished":
            return view
        targets = [PlaceReference.model_validate(t) for t in operation["targets"]]
        context = claim.context.model_copy(deep=True)
        context.current_conditions.origin = (
            Location.model_validate(operation["origin"]) if operation["origin"] else None
        )
        if "driving" in operation["topics"] and context.current_conditions.origin is None:
            pending = PendingRead(
                clarification_id=str(uuid4()),
                topics=operation["topics"],
                target_refs=[t.ref_id for t in targets],
                question="새 추천 장소까지 이동시간을 확인할 출발지를 알려주세요.",
            )
            return await self.store.finish_continuation(claim, pending_read=pending)
        lookup = await self._read(operation["topics"], targets, context)
        return await self.store.finish_continuation(claim, lookup=lookup)

    async def _read(self, topics, targets, context, *, completed=(), comparison_metric=None):
        await checkpoint(
            "looking_up",
            operation={
                "kind": "lookup",
                "topics": topics,
                "targets": [t.model_dump(mode="json") for t in targets],
                "origin": context.current_conditions.origin.model_dump(mode="json")
                if context.current_conditions.origin
                else None,
                "items": [i.model_dump(mode="json") for i in completed],
                "comparison_metric": comparison_metric,
            },
        )
        return await self.tools.read(topics, targets, context, completed=completed)

    async def _retry(self, claim, request, state, source):
        from app.modules.chat_v2.contracts import RunRequest
        from app.modules.chat_v2.dialogue_context import PlaceReference, ReadItem
        from app.schemas.location import Location

        operation = source["operation"] or {"kind": "interpretation"}
        kind = operation["kind"]
        if kind == "action":
            return await self.execute_claim(claim, request, approved_action=operation)
        if kind == "interpretation":
            original = RunRequest.model_validate(source["request"])
            # A retry of a retry retains the original message in the checkpoint.
            message = operation.get("input") or original.input.model_dump(mode="json")
            original = RunRequest.model_validate(
                {
                    **request.model_dump(mode="json"),
                    "input": message,
                    "viewed_context": operation.get("viewed_context")
                    or original.model_dump(mode="json")["viewed_context"],
                }
            )
            return await self.execute_claim(claim, original)
        if kind == "recommendation":
            prior = RecommendationPlan.model_validate(operation["plan"])
            before, after = (
                claim.context.current_conditions.model_dump(),
                prior.effective_conditions.model_dump(),
            )
            plan = RecommendationPlan.model_validate(
                {
                    **prior.model_dump(),
                    "context": claim.context,
                    "changed_fields": [key for key in before if before[key] != after[key]]
                    if prior.scope == "persistent"
                    else [],
                }
            )
            return await self._recommend(claim, plan)
        if kind == "lookup":
            context = claim.context.model_copy(deep=True)
            context.current_conditions.origin = (
                Location.model_validate(operation["origin"]) if operation["origin"] else None
            )
            completed = [ReadItem.model_validate(i) for i in operation.get("items", [])]
            lookup = await self._read(
                operation["topics"],
                [PlaceReference.model_validate(t) for t in operation["targets"]],
                context,
                completed=completed,
                comparison_metric=operation.get("comparison_metric"),
            )
            state.pending_read = None
            comparison = (
                compare_routes(
                    lookup, operation["comparison_metric"], context.current_conditions.origin
                )
                if operation.get("comparison_metric")
                else None
            )
            return await self._answer(claim, state, lookup, comparison=comparison)
        if kind == "search":
            await checkpoint("searching", operation=operation)
            lookup = await self.tools.search_places(
                operation["query"], claim.context.identity.run_id
            )
            state.references = merge_references(state.references, lookup.places)
            state.focus_ref = lookup.places[0].ref_id if len(lookup.places) == 1 else None
            return await self._answer(claim, state, lookup)
        raise ValueError("Unsupported stored retry operation")

    async def _save(self, claim, response, state):
        return await self.store.finish_early(claim, response, state_update=state)

    async def _question(self, claim, state, field, text, *, options=(), warnings=()):
        response = self.composer.needs_input(
            claim.context, NeedsInputIntent(kind="needs_input", question=text, required_field=field)
        )
        response.origin_options = list(options)
        response.warnings = list(warnings)
        return await self._save(claim, response, state)

    async def _answer(self, claim, state, lookup, *, comparison=None):
        if state.pending_question and state.pending_question.field in {"target", "lookup_origin"}:
            state.pending_question = None
        response = ChatResponse(
            identity=claim.context.identity,
            kind="answer",
            status="partial"
            if comparison and comparison.outcome == "incomplete" and lookup.outcome != "error"
            else "failed"
            if lookup.outcome == "error"
            else "partial"
            if lookup.outcome == "partial"
            else "succeeded",
            answer=explain_lookup(lookup)
            + (" " + comparison_text(comparison, claim.context.references) if comparison else ""),
            conditions=claim.context.current_conditions,
            condition_version=claim.context.condition_version,
            lookup=lookup,
            comparison=comparison,
        )
        return await self._save(claim, response, state)

    async def _fail(self, claim, state, code, message):
        return await self._save(
            claim,
            self.composer.failed(
                claim.context,
                StageError(
                    code=code,
                    safe_message=message,
                    retryable=code in {"INTERPRETATION_FAILED", "RECOMMENDATION_FAILED"},
                ),
            ),
            state,
        )
