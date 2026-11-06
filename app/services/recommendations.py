"""Structured R01 orchestration, callable directly by a future chat adapter."""

import asyncio
import copy
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from app.clients.errors import ProviderError
from app.modules.ai.explanation import explain
from app.modules.recommendation.ranking import rank_candidates
from app.modules.recommendation.sessions import SearchSession
from app.schemas.recommendation import Candidate
from app.schemas.recommendations import Conditions, RecommendationEnvelope

PURPOSE_QUERIES = {"food": "음식점", "cafe": "카페", "walk": "공원", "exhibition": "전시관"}


class RequestFailure(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message


def information(value, source, *, reason=None, observed=None, collected=None):
    return dict(
        value=value,
        status="available" if value is not None else "unavailable",
        freshness="unknown",
        observed_at=observed,
        collected_at=collected,
        source=source,
        reason_code=reason,
    )


class StructuredRecommendationService:
    def __init__(self, model, kakao, evidence, sessions):
        self.model, self.kakao, self.evidence, self.sessions = model, kakao, evidence, sessions

    async def execute(self, request):
        # Serialize state updates and identical requests; no hidden paid retry.
        async with self.sessions.lock:
            self.sessions.prune()
            key = (str(request.trip_id), str(request.request_id))
            fingerprint = json.dumps(request.model_dump(mode="json"), sort_keys=True)
            cached = self.sessions.requests.get(key)
            if cached:
                old_fingerprint, response = cached[1]
                if fingerprint != old_fingerprint:
                    return self.failure(
                        request,
                        409,
                        "REQUEST_ID_CONFLICT",
                        "같은 request_id에 다른 요청을 보낼 수 없습니다.",
                    )
                return copy.deepcopy(response)
            update = dict(
                applied=False,
                conditions=request.current_conditions.model_dump(mode="json"),
                version=request.condition_version,
                changes=[],
            )
            try:
                async with asyncio.timeout(90):
                    response = await self._execute(request, update)
            except RequestFailure as exc:
                response = self.failure(request, exc.status, exc.code, exc.message, update)
            except ValidationError:
                response = self.failure(
                    request, 422, "VALIDATION_ERROR", "입력 조건을 확인해 주세요.", update
                )
            except ProviderError as exc:
                status = 429 if exc.code == "kakao_quota" else exc.status_code
                code = {
                    429: "RATE_LIMITED",
                    504: "PROVIDER_TIMEOUT",
                    503: "SERVICE_UNAVAILABLE",
                }.get(status, "PROVIDER_ERROR")
                response = self.failure(request, status, code, exc.message, update)
            except SQLAlchemyError:
                response = self.failure(
                    request, 503, "DATA_UNAVAILABLE", "등록 자료를 조회할 수 없습니다.", update
                )
            except TimeoutError:
                response = self.failure(
                    request, 504, "PROVIDER_TIMEOUT", "추천 처리 시간이 초과되었습니다.", update
                )
            except Exception:
                response = self.failure(
                    request, 500, "INTERNAL_ERROR", "추천 처리 중 오류가 발생했습니다.", update
                )
            self.sessions.put(self.sessions.requests, key, (fingerprint, copy.deepcopy(response)))
            return response

    def response(
        self,
        request,
        update,
        kind,
        message,
        *,
        result=None,
        routes="skipped",
        explanation="skipped",
        warnings=None,
        error=None,
        status=200,
    ):
        body = RecommendationEnvelope.model_validate(
            dict(
                request_id=request.request_id,
                server_time=datetime.now(UTC),
                error=error,
                data=dict(
                    trip_id=request.trip_id,
                    base_condition_version=request.condition_version,
                    kind=kind,
                    message=message,
                    condition_update=update,
                    stages=dict(interpretation="skipped", routes=routes, explanation=explanation),
                    result=result,
                    clarification=None,
                    answer=None,
                    warnings=warnings or [],
                ),
            )
        )
        return status, body.model_dump(mode="json")

    def failure(self, request, status, code, message, update=None):
        update = update or dict(
            applied=False,
            conditions=request.current_conditions.model_dump(mode="json"),
            version=request.condition_version,
            changes=[],
        )
        return self.response(
            request,
            update,
            "failure",
            message,
            status=status,
            error=dict(code=code, message=message, retryable=status in {429, 502, 503, 504}),
        )

    async def _execute(self, request, update):
        trip_key = str(request.trip_id)
        saved = self.sessions.trips.get(trip_key)
        old = saved[1] if saved else SearchSession()
        if old.conditions is not None and (
            old.version != request.condition_version or old.conditions != request.current_conditions
        ):
            raise RequestFailure(
                409, "CONDITION_VERSION_CONFLICT", "현재 여행의 조건과 버전을 확인해 주세요."
            )
        values = request.current_conditions.model_dump()
        values.update(request.conditions_patch.model_dump(exclude_unset=True))
        conditions = Conditions.model_validate(values)
        if conditions.origin is None or not conditions.purposes:
            raise RequestFailure(422, "VALIDATION_ERROR", "출발지와 추천 목적이 필요합니다.")
        coordinates = conditions.origin.coordinates
        region = await self.kakao.region(coordinates.longitude, coordinates.latitude)
        if region not in {"seoul", "incheon", "gyeonggi"}:
            raise RequestFailure(
                422, "ORIGIN_OUT_OF_SERVICE_AREA", "서울·인천·경기 출발지를 선택해 주세요."
            )
        conditions.origin.region, conditions.origin.supported = region, True
        before = request.current_conditions.model_dump(mode="json")
        after = conditions.model_dump(mode="json")
        changes = [
            dict(field=k, before=before[k], after=v) for k, v in after.items() if before[k] != v
        ]
        version = request.condition_version + bool(changes)
        update.update(applied=True, conditions=after, version=version, changes=changes)
        state = (
            copy.deepcopy(old)
            if old.conditions == conditions
            else SearchSession(shown=set(old.shown))
        )
        state.conditions, state.version = conditions, version
        state.shown.update(request.excluded_place_ids)
        self.sessions.put(self.sessions.trips, trip_key, copy.deepcopy(state))
        await self.evidence.load()
        eligible, failed = [], []
        route_successes = 0
        evaluated = set()
        queries = [
            (p.purpose, p.keyword or PURPOSE_QUERIES[p.purpose]) for p in conditions.purposes
        ]
        origin = SimpleNamespace(x=coordinates.longitude, y=coordinates.latitude)
        semaphore = asyncio.Semaphore(5)

        async def route(place):
            async with semaphore:
                try:
                    return await self.kakao.route(origin, place)
                except ProviderError as exc:
                    return exc

        async def evaluate(entries):
            nonlocal route_successes
            unique = []
            for place, purpose in entries:
                pid = "kakao:" + place.id
                if pid in state.shown or pid in evaluated:
                    continue
                evaluated.add(pid)
                # Validate destination region from coordinates, not display labels.
                if await self.kakao.region(place.x, place.y) != "seoul":
                    continue
                unique.append((place, purpose))
            answers = await asyncio.gather(*(route(p) for p, _ in unique))
            for (place, purpose), answer in zip(unique, answers, strict=True):
                if isinstance(answer, ProviderError):
                    if answer.code in {"kakao_not_configured", "kakao_auth", "kakao_quota"}:
                        raise answer
                    failed.append((place, purpose))
                    continue
                seconds, distance = answer
                route_successes += 1
                if (
                    conditions.max_travel_seconds is not None
                    and seconds > conditions.max_travel_seconds
                ):
                    continue
                traffic, parking = await self.evidence.for_place(place)
                candidate = Candidate(
                    place=place, duration_seconds=seconds, traffic=traffic, parking=parking
                )
                eligible.append((candidate, purpose, distance))

        await evaluate(state.remaining)
        pages = 0
        while len(eligible) < 3 and pages < 3:
            pending = [(p, q) for p, q in queries if p not in state.ended]
            if not pending:
                break
            for purpose, query in pending:
                if pages >= 3:
                    break
                page = state.pages.get(purpose, 1)
                places, end = await self.kakao.search(
                    SimpleNamespace(query=query, x=origin.x, y=origin.y), page
                )
                state.pages[purpose] = page + 1
                if end or page >= 45:
                    state.ended.add(purpose)
                pages += 1
                entries = [(p, purpose) for p in places if p.id not in state.searched]
                state.searched.update(p.id for p in places)
                await evaluate(entries)
        warnings = []
        if failed:
            warnings.append(
                dict(
                    code="ROUTE_PARTIAL_FAILURE" if route_successes else "ROUTE_ALL_FAILED",
                    message="일부 경로를 확인하지 못했습니다."
                    if route_successes
                    else "모든 경로를 확인하지 못했습니다.",
                )
            )
        routes = (
            "partial"
            if failed and route_successes
            else "failed"
            if failed
            else "ok"
            if route_successes
            else "skipped"
        )
        more = any(p not in state.ended for p, _ in queries)
        if more and len(eligible) < 3:
            warnings.append(
                dict(code="SEARCH_INCOMPLETE", message="추가 요청에서 더 탐색할 수 있습니다.")
            )
        now = datetime.now(UTC).isoformat()
        if not eligible:
            state.remaining = failed
            self.sessions.put(self.sessions.trips, trip_key, state)
            if failed and not route_successes:
                cards = [self.card(p, purpose, None, None, None, now) for p, purpose in failed[:3]]
                return self.response(
                    request,
                    update,
                    "general_candidates",
                    "이동시간을 확인하지 못한 일반 후보입니다.",
                    result=self.result(after, cards, now),
                    routes=routes,
                    warnings=warnings,
                )
            return self.response(
                request,
                update,
                "no_candidates",
                "조건에 맞는 추천 후보가 없습니다.",
                routes=routes,
                warnings=warnings,
            )
        extras = {c.place.id: (purpose, distance) for c, purpose, distance in eligible}
        ranked = rank_candidates([c for c, _, _ in eligible])
        state.remaining = [(c.place, extras[c.place.id][0]) for c in ranked[3:]] + failed
        selected, source = await explain(self.model, ranked[:3])
        cards = [
            self.card(c.place, extras[c.place.id][0], c, rank, extras[c.place.id][1], now, source)
            for rank, c in enumerate(selected, 1)
        ]
        state.shown.update("kakao:" + c.place.id for c in selected)
        self.sessions.put(self.sessions.trips, trip_key, state)
        return self.response(
            request,
            update,
            "recommendation",
            "조건에 맞는 장소를 추천합니다.",
            result=self.result(after, cards, now),
            routes=routes,
            explanation="ok" if source == "model_grounded" else "fallback",
            warnings=warnings,
        )

    @staticmethod
    def result(conditions, cards, now):
        # Historical context-token contract is deferred; never invent a working token.
        return dict(
            result_id=str(uuid4()),
            created_at=now,
            conditions=conditions,
            context_token=None,
            context_expires_at=None,
            cards=cards,
        )

    @staticmethod
    def card(place, purpose, candidate, rank, distance, now, source="none"):
        place_value = dict(
            place_id="kakao:" + place.id,
            name=place.name,
            purpose=purpose,
            category_code=place.category_code,
            category_name=place.category_name,
            coordinates=dict(latitude=place.y, longitude=place.x),
            address=place.address or None,
            road_address=place.road_address,
            phone=place.phone,
            provider_url=str(place.place_url) if place.place_url else None,
        )
        route = information(
            dict(duration_seconds=candidate.duration_seconds, distance_meters=distance)
            if candidate
            else None,
            "kakao_mobility",
            reason=None if candidate else "ROUTE_FAILED",
            collected=now,
        )
        evidence = []
        if candidate:
            evidence.append(
                dict(
                    type="eta",
                    text=f"자동차 이동시간 {candidate.duration_seconds}초",
                    information=route,
                )
            )
            if "도착지 주변 도로" in candidate.explanation:
                traffic = candidate.traffic
                evidence.append(
                    dict(
                        type="traffic",
                        text=f"도착지 주변 도로: {traffic.status}",
                        information=information(
                            dict(status=traffic.status, road_ids=traffic.road_ids)
                            if traffic.status != "확인 불가"
                            else None,
                            traffic.source,
                            reason="NO_RECORD" if traffic.status == "확인 불가" else None,
                            observed=traffic.source_time.isoformat()
                            if traffic.source_time
                            else None,
                        ),
                    )
                )
            if candidate.parking:
                p = candidate.parking
                evidence.append(
                    dict(
                        type="parking",
                        text=f"등록 주차장까지 직선거리 {p.distance_m:g}m",
                        information=information(
                            dict(distance_meters=p.distance_m, name=p.name),
                            p.source,
                            observed=p.source_time.isoformat() if p.source_time else None,
                        ),
                    )
                )
        parking = dict(
            status="unavailable",
            reason_code="FACILITY_ID_UNAVAILABLE"
            if candidate and candidate.parking
            else "NO_RECORD",
            checked_at=now,
            search_radius_meters=1000 if candidate else None,
            nearest_distance_meters=candidate.parking.distance_m
            if candidate and candidate.parking
            else None,
            items=[],
            total_count=None,
            next_cursor=None,
        )
        return dict(
            rank=rank,
            place=information(
                place_value,
                "kakao_local",
                collected=place.collected_at.isoformat() if place.collected_at else None,
            ),
            route=route,
            parking=parking,
            reason=dict(
                text=candidate.explanation
                if candidate
                else "자동차 이동시간과 순위는 확인되지 않았습니다.",
                source=("ai" if source == "model_grounded" else "fallback")
                if candidate
                else "none",
                evidence=evidence,
            ),
        )
