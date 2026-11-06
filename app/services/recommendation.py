"""Connect interpretation, providers, pure ranking and expiring search state."""

import asyncio
import time
from pathlib import Path

from app.modules.ai.explanation import explain
from app.modules.ai.interpretation import merge_conditions
from app.modules.recommendation.errors import ProviderError
from app.modules.recommendation.ranking import DEFAULT_POLICY, rank_candidates
from app.schemas.recommendation import (
    Candidate,
    ChatResponse,
    Conditions,
    Interpretation,
    SearchState,
)


class RecommendationService:
    def __init__(self, model, kakao, evidence, codec, policy=DEFAULT_POLICY):
        self.model, self.kakao, self.evidence, self.codec = model, kakao, evidence, codec
        self.policy = policy

    async def recommend(self, request):
        previous = self.codec.decode(request.state) if request.state else None
        current = previous.conditions if previous else request.conditions
        if previous and request.conditions is not None:
            # Explicit UI edits are partial: omitted defaults must not erase previous values.
            values = current.model_dump()
            updates = request.conditions.model_dump(exclude_unset=True)
            values.update(updates)
            current = Conditions.model_validate(values)
        interpretation = await self.model.complete(
            Path(__file__)
            .parents[1]
            .joinpath("modules/ai/prompts/interpretation.txt")
            .read_text("utf-8"),
            {
                "message": request.message,
                "current_conditions": current.model_dump() if current else None,
            },
            Interpretation,
        )
        interpretation = Interpretation.model_validate(interpretation)
        if interpretation.request_type == "recommend_more" and previous is None:
            raise ValueError("추가 추천에는 이전 응답의 state가 필요합니다.")
        conditions = merge_conditions(current, interpretation)
        # Explicit selected coordinates take precedence over model-extracted labels.
        if request.conditions is not None and request.conditions.x is not None:
            conditions = conditions.model_copy(
                update={"x": request.conditions.x, "y": request.conditions.y}
            )
        origin_name = interpretation.changes.origin_name
        kind = interpretation.request_type
        base = {"request_type": kind, "conditions": conditions}
        if kind == "new_recommendation" and not interpretation.changes.model_dump(
            exclude_none=True
        ):
            return ChatResponse(
                **base,
                status="needs_clarification",
                message="자동차로 방문할 장소의 종류와 출발지를 알려주세요.",
            )
        if not conditions.query:
            return ChatResponse(
                **base, status="needs_clarification", message="검색어를 알려주세요."
            )
        if conditions.x is None:
            if not origin_name:
                return ChatResponse(
                    **base, status="needs_clarification", message="출발지를 알려주세요."
                )
            options = await self.kakao.origins(origin_name)
            exact = [p for p in options if p.name == origin_name]
            if len(exact) != 1:
                pending = SearchState(conditions=conditions, expires_at=int(time.time()) + 1800)
                return ChatResponse(
                    **base,
                    status="needs_clarification",
                    origin_options=options,
                    state=self.codec.encode(pending),
                    message="출발지 검색 결과에서 정확한 위치를 선택해 주세요.",
                )
            conditions = conditions.model_copy(update={"x": exact[0].x, "y": exact[0].y})
            base["conditions"] = conditions
        if await self.kakao.region(conditions.x, conditions.y) not in {
            "seoul",
            "incheon",
            "gyeonggi",
        }:
            raise ValueError("서울·인천·경기 출발지를 선택해 주세요.")
        reset = (
            previous is None or previous.conditions != conditions or kind == "new_recommendation"
        )
        state = (
            SearchState(conditions=conditions, expires_at=int(time.time()) + 1800)
            if reset
            else previous.model_copy(deep=True)
        )
        await self.evidence.load()
        shown = set(state.shown_place_ids)
        searched = set(state.searched_place_ids)
        eligible, retry = [], []
        transient_errors = []
        evaluated = set()
        semaphore = asyncio.Semaphore(5)

        async def route(place):
            async with semaphore:
                return await self.kakao.duration(conditions, place)

        async def evaluate(places):
            unique = []
            for place in places:
                if place.id not in shown and place.id not in evaluated:
                    evaluated.add(place.id)
                    if await self.kakao.region(place.x, place.y) == "seoul":
                        unique.append(place)
            durations = await asyncio.gather(*(route(p) for p in unique), return_exceptions=True)
            for place, seconds in zip(unique, durations, strict=True):
                if isinstance(seconds, ProviderError):
                    if seconds.code in {"kakao_not_configured", "kakao_auth", "kakao_quota"}:
                        raise seconds
                    if seconds.code != "route_not_found":
                        transient_errors.append(seconds)
                        retry.append(place)
                    continue
                if isinstance(seconds, Exception):
                    raise seconds
                if type(seconds) is not int or seconds < 0:
                    transient_errors.append(
                        ProviderError(502, "route_invalid", "이동시간을 확인할 수 없습니다.")
                    )
                    retry.append(place)
                    continue
                if seconds > conditions.max_minutes * 60:
                    continue
                traffic, parking = await self.evidence.for_place(place)
                eligible.append(
                    Candidate(
                        place=place, duration_seconds=seconds, traffic=traffic, parking=parking
                    )
                )

        await evaluate(state.remaining)
        pages = 0
        while len(eligible) < 3 and not state.is_end and pages < self.policy.max_pages_per_request:
            if state.next_page > 45:
                state.is_end = True
                break
            places, is_end = await self.kakao.search(conditions, state.next_page)
            unseen = [p for p in places if p.id not in searched and p.id not in shown]
            searched.update(p.id for p in places)
            state.next_page += 1
            state.is_end = is_end or state.next_page > 45
            pages += 1
            await evaluate(unseen)
        if not eligible and transient_errors:
            raise transient_errors[0]
        ranked = rank_candidates(eligible, self.policy)
        selected = ranked[:3]
        state.remaining = [c.place for c in ranked[3:]] + retry
        shown.update(c.place.id for c in selected)
        state.shown_place_ids = sorted(shown)
        state.searched_place_ids = sorted(searched)
        results, explanation_source = await explain(self.model, selected)
        more = bool(state.remaining) or not state.is_end
        incomplete = len(selected) < 3 and more
        status = "search_incomplete" if incomplete else ("ok" if more else "exhausted")
        message = (
            "추가 탐색 가능한 후보가 있습니다. 다른 곳 추천을 요청해 주세요."
            if incomplete
            else (
                "현재 검색 후보를 모두 확인했습니다. 검색어나 범위를 바꿔보세요."
                if not more
                else "추가 추천에서 아직 보여드리지 않은 장소를 확인할 수 있습니다."
            )
        )
        return ChatResponse(
            **base,
            status=status,
            recommendations=results,
            more_available=more,
            state=self.codec.encode(state),
            message=message,
            explanation_source=explanation_source,
        )
