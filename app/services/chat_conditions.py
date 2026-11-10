"""Server-owned drafts for general condition questions, separate from origin choices."""

from dataclasses import dataclass

from app.modules.ai.interpretation import HeadcountHint
from app.schemas.recommendations import Clarification, Conditions, ConditionsPatch, WarningMessage
from app.services.origin_context import FIELD_LABELS, missing_conditions

OPTIONAL_FIELDS = {"headcount", "group_type", "age_groups", "max_travel_seconds"}


@dataclass
class ConditionDraft:
    version: int
    conditions: Conditions
    clarification: Clarification
    headcount_hint: HeadcountHint | None = None
    unanswered_turns: int = 0
    paused: bool = False

    def context(self):
        return {
            "status": "paused" if self.paused else "active",
            "unanswered_turns": self.unanswered_turns,
            "headcount_hint": self.headcount_hint.model_dump() if self.headcount_hint else None,
            "clarification": self.clarification.model_dump(mode="json"),
        }


def current_draft(request, work):
    draft = work.condition_draft
    if (
        not isinstance(draft, ConditionDraft)
        or work.expired
        or draft.version != request.condition_version
        or draft.conditions != request.current_conditions
    ):
        return None
    return draft


def is_condition_question(question):
    return (
        question is not None
        and question.origin_context is None
        and set(question.required_fields) <= set(Conditions.model_fields)
    )


def resolve_conditions(request, parsed, draft):
    """Resolve only this turn's answers; missing optional values do not imply an answer."""
    patch = draft.clarification.pending_patch.model_dump(exclude_unset=True) if draft else {}
    waiting = set(draft.clarification.required_fields) if draft else set()
    changed = parsed.patch().model_dump(exclude_unset=True)
    hint = draft.headcount_hint if draft else None
    answered = waiting & set(changed)
    patch.update(changed)
    waiting.difference_update(changed)
    waiting.update(set(parsed.clarification_fields) - set(changed))
    if "headcount" in changed:
        hint = None
    if parsed.headcount_hint is not None:
        hint = parsed.headcount_hint.model_copy(deep=True)
        patch.pop("headcount", None)
        waiting.add("headcount")
    warnings = []
    missing = missing_conditions(
        Conditions.model_validate(request.current_conditions.model_dump() | patch)
    )
    if parsed.dialogue_action == "proceed" and not missing:
        if "headcount" in waiting and hint is not None and hint.minimum is not None:
            patch["headcount"] = hint.minimum
            warnings.append(
                WarningMessage(
                    code="HEADCOUNT_BASELINE_ASSUMED",
                    message=f"말씀하신 ‘{hint.quote}’에서 "
                    f"{hint.minimum}명을 추천 기준으로 사용했어요. "
                    "정확한 인원이 정해지면 바꿀 수 있어요.",
                )
            )
            waiting.remove("headcount")
        skipped = waiting & OPTIONAL_FIELDS
        for field in skipped:
            patch.pop(field, None)
        if skipped:
            warnings.append(
                WarningMessage(
                    code="UNRESOLVED_OPTIONAL_CHANGES_SKIPPED",
                    message="추가 확인 중인 "
                    + ", ".join(FIELD_LABELS[f] for f in sorted(skipped))
                    + " 변경은 적용하지 않고 진행했어요.",
                )
            )
        waiting.difference_update(OPTIONAL_FIELDS)
    patch = ConditionsPatch.model_validate(patch)
    merged = Conditions.model_validate(
        request.current_conditions.model_dump() | patch.model_dump(exclude_unset=True)
    )
    required = list(dict.fromkeys(missing_conditions(merged) + sorted(waiting)))
    question = (
        ", ".join(FIELD_LABELS[field] for field in required) + "을 알려 주세요."
        if required
        else parsed.question or "추가 조건을 확인해 주세요."
    )
    if changed and required:
        question = "말씀하신 변경은 초안으로 보관했어요. " + question
    updated = ConditionDraft(
        version=request.condition_version,
        conditions=request.current_conditions.model_copy(deep=True),
        clarification=Clarification(
            question=question, required_fields=required, pending_patch=patch, choices=[]
        ),
        headcount_hint=hint,
        unanswered_turns=0 if answered else (draft.unanswered_turns + 1 if draft else 0),
    )
    return updated, warnings
