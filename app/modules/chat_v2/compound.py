"""Server normalization for temporary criteria and explicitly allowed alternatives."""

from uuid import uuid4

from app.modules.chat_v2.contracts import RecommendationIntent, RecommendationPlan
from app.modules.chat_v2.predicates import Predicate, PredicateGroup, RecommendationOptions
from app.modules.chat_v2.results import StageError
from app.modules.chat_v2.validation import StageFailure


def configure_draft(draft, decision, context):
    if isinstance(decision, RecommendationIntent) and decision.use_previous:
        flow = context.active_flow
        if flow is None:
            raise StageFailure(
                StageError(
                    code="CONFLICT",
                    safe_message="이어갈 임시 추천을 선택해 주세요.",
                    retryable=False,
                )
            )
        draft.scope, draft.flow_id = "request_only", flow.flow_id
        draft.conditions = flow.conditions.model_copy(deep=True)
        draft.search_area = flow.search_area
        draft.constraints = flow.constraints
        draft.flow_excluded = list(flow.excluded)
    elif hasattr(decision, "scope") and "scope" in decision.model_fields_set:
        draft.scope = decision.scope
    if isinstance(decision, RecommendationIntent):
        for field in ("constraints", "commit_guard", "alternative", "after"):
            value = getattr(decision, field)
            if value is not None:
                setattr(draft, field, value.model_copy(deep=True))
    if draft.scope == "request_only" and draft.flow_id is None:
        draft.flow_id = uuid4()
    if draft.scope == "persistent":
        draft.flow_id, draft.flow_excluded = None, []


def plan_from_draft(draft, context):
    before, after = context.current_conditions.model_dump(), draft.conditions.model_dump()
    return RecommendationPlan(
        context=context,
        search_area=draft.search_area,
        effective_conditions=draft.conditions,
        changed_fields=[key for key in before if before[key] != after[key]]
        if draft.scope == "persistent"
        else [],
        flow_id=draft.flow_id,
        flow_excluded=draft.flow_excluded,
        **draft.model_dump(include=set(RecommendationOptions.model_fields)),
    )


def omit_parking(predicate):
    if predicate is None:
        return None
    terms = []
    for term in predicate.terms:
        if isinstance(term, PredicateGroup):
            leaves = [a for a in term.terms if a.kind != "registered_parking_exists"]
            if leaves:
                terms.append(term.model_copy(update={"terms": leaves}))
        elif term.kind != "registered_parking_exists":
            terms.append(term)
    return Predicate(operator=predicate.operator, terms=terms) if terms else None


def alternative_plan(plan):
    alternative = plan.alternative
    values = plan.effective_conditions.model_dump()
    if alternative.max_travel_seconds is not None:
        values["max_travel_seconds"] = alternative.max_travel_seconds
    if alternative.purposes is not None:
        values["purposes"] = [p.model_dump() for p in alternative.purposes]
    changed = [
        key for key in values if values[key] != plan.context.current_conditions.model_dump()[key]
    ]
    return RecommendationPlan.model_validate(
        {
            **plan.model_dump(),
            "effective_conditions": values,
            "alternative": None,
            "constraints": omit_parking(plan.constraints)
            if alternative.omit_parking
            else plan.constraints,
            "changed_fields": changed if plan.scope == "persistent" else [],
        }
    )
