"""Compare fresh routes only when all places share a confirmed origin."""

from datetime import UTC, datetime, timedelta

from app.modules.chat_v2.dialogue_context import ComparisonResult


def compare_routes(lookup, metric, origin):
    rows = [item for item in lookup.items if item.topic == "driving"]
    values = {}
    now = datetime.now(UTC)
    for item in rows:
        value = None
        if item.status == "ok" and isinstance(item.data, dict):
            route = item.data.get("route", {})
            try:
                at = datetime.fromisoformat(route["collected_at"])
                if (
                    origin is not None
                    and item.data.get("origin") == origin.coordinates.model_dump(mode="json")
                    and route["status"] == "available"
                    and timedelta(0) <= now - at <= timedelta(seconds=120)
                ):
                    value = route["value"][metric]
            except KeyError, ValueError, TypeError:
                pass
        values[item.place_ref] = value
    complete = len(values) >= 2 and all(v is not None for v in values.values())
    best = [ref for ref, v in values.items() if v == min(values.values())] if complete else []
    return ComparisonResult(
        metric=metric,
        unit="seconds" if metric == "duration_seconds" else "meters",
        origin_label=origin.label if origin else "미확인",
        values=values,
        outcome="complete" if complete else "incomplete",
        best_refs=best,
        reason_code=None if complete else "COMMON_FRESH_ROUTE_REQUIRED",
    )


def comparison_text(comparison, references):
    if comparison.outcome == "incomplete":
        return "같은 출발지와 유효 시각의 경로를 모두 확인하지 못해 우열을 정하지 않았어요."
    names = {r.ref_id: r.place.name for r in references}
    winners = ", ".join(names.get(ref, "선택한 장소") for ref in comparison.best_refs)
    return (
        f"{comparison.origin_label} 출발 기준으로 {winners}의 "
        + (
            "이동시간이 가장 짧아요."
            if comparison.metric == "duration_seconds"
            else "주행거리가 가장 짧아요."
        )
        + (" 같은 값인 장소를 함께 표시했어요." if len(comparison.best_refs) > 1 else "")
    )
