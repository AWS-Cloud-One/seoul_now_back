"""Select server-owned evidence clauses; never let a model rank or invent facts."""

import math
from pathlib import Path
from typing import Literal

from pydantic import Field

from app.core.exceptions import ServiceError
from app.schemas.common import ContractModel


class ExplanationItem(ContractModel):
    place_id: str
    facts: list[Literal["eta", "parking"]] = Field(min_length=1, max_length=2)


class ExplanationPlan(ContractModel):
    items: list[ExplanationItem] = Field(min_length=1, max_length=3)


def clauses(candidate):
    facts = {"eta": f"예상 자동차 이동시간 약 {math.ceil(candidate.duration_seconds / 60)}분"}
    if candidate.parking and candidate.parking.nearest_distance_meters is not None:
        facts["parking"] = (
            f"주변 등록 주차장까지 직선거리 {candidate.parking.nearest_distance_meters:g}m"
            " (빈자리 미확인)"
        )
    return facts


async def explain(model, candidates):
    available = [clauses(candidate) for candidate in candidates]
    selections = [list(facts) for facts in available]
    source = "fallback"
    if model is not None and candidates:
        try:
            plan = ExplanationPlan.model_validate(
                await model.complete(
                    Path(__file__)
                    .with_name("prompts")
                    .joinpath("explanation.txt")
                    .read_text("utf-8"),
                    {
                        "ordered_candidates": [
                            {"place_id": candidate.place.place_id, "facts": facts}
                            for candidate, facts in zip(candidates, available, strict=True)
                        ]
                    },
                    ExplanationPlan,
                )
            )
            if [item.place_id for item in plan.items] != [
                candidate.place.place_id for candidate in candidates
            ]:
                raise ValueError("changed order")
            for item, facts in zip(plan.items, available, strict=True):
                if (
                    "eta" not in item.facts
                    or len(set(item.facts)) != len(item.facts)
                    or not set(item.facts).issubset(facts)
                ):
                    raise ValueError("unsupported evidence")
            selections, source = [item.facts for item in plan.items], "ai"
        except ServiceError, ValueError, TimeoutError:
            pass
    return [
        ("; ".join(facts[key] for key in selected) + ".", selected)
        for facts, selected in zip(available, selections, strict=True)
    ], source
