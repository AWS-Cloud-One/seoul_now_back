"""Merge only explicit, validated changes; models cannot set coordinates or pages."""

from app.schemas.recommendation import Conditions, Interpretation


def merge_conditions(current: Conditions | None, result: Interpretation) -> Conditions:
    patch = result.changes.model_dump(exclude_none=True)
    if result.request_type == "recommend_more":
        if current is None or patch:
            raise ValueError("추가 추천은 기존 조건이 필요하며 조건을 변경할 수 없습니다.")
        return current.model_copy(deep=True)
    if result.request_type == "update_conditions" and current is None:
        raise ValueError("변경할 기존 검색 조건이 없습니다.")
    values = (current or Conditions()).model_dump()
    if "origin_name" in patch and patch["origin_name"] != values["origin_name"]:
        values.update(x=None, y=None)
    values.update(patch)
    return Conditions.model_validate(values)
