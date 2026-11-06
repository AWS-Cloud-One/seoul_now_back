"""GPT selects evidence clauses; rendering keeps every number server-owned."""

import math
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError

from app.clients.errors import ProviderError
from app.schemas.recommendation import StrictModel


class ExplanationItem(StrictModel):
    place_id: str
    facts: list[Literal["eta", "traffic", "parking"]] = Field(min_length=1, max_length=3)


class ExplanationPlan(StrictModel):
    items: list[ExplanationItem] = Field(min_length=1, max_length=3)


def clauses(candidate):
    facts = {
        "eta": f"예상 이동시간 약 {math.ceil(candidate.duration_seconds / 60)}분",
        "traffic": f"도착지 주변 도로: {candidate.traffic.status}",
    }
    if candidate.parking is not None:
        p = candidate.parking
        facts["parking"] = f"주변 {p.name}: 확인된 잔여 {p.available_spaces}면"
    return facts


async def explain(model, candidates):
    if not candidates:
        return [], "none"
    available = [clauses(c) for c in candidates]
    selections = [list(facts) for facts in available]
    source = "template"
    try:
        plan = await model.complete(
            Path(__file__).with_name("prompts").joinpath("explanation.txt").read_text("utf-8"),
            {
                "ordered_candidates": [
                    {"place_id": c.place.id, "facts": facts}
                    for c, facts in zip(candidates, available, strict=True)
                ]
            },
            ExplanationPlan,
        )
        plan = ExplanationPlan.model_validate(plan)
        if [i.place_id for i in plan.items] != [c.place.id for c in candidates]:
            raise ValueError("changed order")
        for item, facts in zip(plan.items, available, strict=True):
            if (
                "eta" not in item.facts
                or len(set(item.facts)) != len(item.facts)
                or not set(item.facts).issubset(facts)
            ):
                raise ValueError("unsupported facts")
        selections = [item.facts for item in plan.items]
        source = "model_grounded"
    except ProviderError, ValidationError, ValueError:
        pass
    return [
        c.model_copy(update={"explanation": "; ".join(facts[key] for key in selected) + "."})
        for c, facts, selected in zip(candidates, available, selections, strict=True)
    ], source
