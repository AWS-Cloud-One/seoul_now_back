"""Question choices are deterministic suggestions, never model-generated commands."""

from app.modules.chat_v2.presentation_contracts import ChoiceDefinition, QuestionDefinition


def pending_ids(state):
    return {
        q.clarification_id for q in (state.pending_question, state.pending_read) if q is not None
    }


def question_definition(state, field):
    lookup = field in {"target", "lookup_origin"} and state.pending_read is not None
    pending = state.pending_read if lookup else state.pending_question
    if pending is None or (not lookup and pending.field != field):
        return None
    scope = "lookup" if field in {"target", "lookup_origin"} else pending.scope
    choices = []

    def choice(key, label, decision, description=None):
        choices.append(
            ChoiceDefinition(
                key=key,
                label=label,
                description=description,
                decision={**decision, "evidence_text": label},
            )
        )

    if field == "headcount":
        for n in range(1, 7):
            choice(str(n), f"{n}명", {"kind": "change_headcount", "headcount": n, "scope": scope})
    elif field == "purposes":
        for value, label in [
            ("cafe", "카페"),
            ("food", "식사"),
            ("walk", "산책"),
            ("exhibition", "전시"),
        ]:
            choice(
                value,
                label,
                {"kind": "recommend", "purposes": [{"purpose": value}], "scope": scope},
            )
    elif field in {"origin", "lookup_origin"}:
        options = (
            pending.origin_options if lookup else state.draft.origin_options if state.draft else []
        )
        for option in options:
            choice(
                option.option_id,
                option.location.label,
                {
                    "kind": "select_origin",
                    "position": option.position,
                    "for_lookup": lookup,
                },
                option.location.address,
            )
    elif field == "search_area" and state.draft:
        for option in state.draft.destination_options:
            choice(
                str(option.position),
                option.region.label,
                {"kind": "select_search_area", "position": option.position},
                option.description,
            )
    elif field == "target" and lookup and pending.comparison_metric is None:
        # Offer identified places, never infer the place intended by an ambiguous pronoun.
        for ref in state.references[-5:]:
            choice(
                ref.ref_id,
                ref.place.name,
                {
                    "kind": "read",
                    "topics": pending.topics,
                    "targets": [{"kind": "reference", "ref_id": ref.ref_id}],
                },
                f"{ref.position}번 · {ref.place.address or '주소 미확인'}",
            )
            choices[-1].target_ref = ref.ref_id
            choices[-1].collection_id = ref.collection_id
    return QuestionDefinition(
        clarification_id=pending.clarification_id,
        prompt=pending.question,
        required_field=field,
        scope=scope,
        expected_answer_type="choice" if choices else "integer" if field == "headcount" else "text",
        choices=choices,
    )
