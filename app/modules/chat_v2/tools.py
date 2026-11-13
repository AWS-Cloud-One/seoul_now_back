"""P2 read adapters. Live facts and historical recommendation facts stay separate."""

import asyncio
import hashlib

from sqlalchemy.exc import SQLAlchemyError

from app.clients.area_extras import SeoulAreaExtras
from app.clients.kakao import KakaoError, KakaoLocalClient
from app.clients.query_kakao import KakaoQueryClient
from app.core.exceptions import ServiceError
from app.modules.chat_v2.control import checkpoint
from app.modules.chat_v2.dialogue_context import (
    LookupResult,
    PlaceReference,
    ReadItem,
    SourceReference,
)
from app.modules.chat_v2.references import owned_recommendation
from app.repositories.parking_nearby import ParkingNearbyRepository
from app.repositories.recommendation_geography import RecommendationGeographyRepository
from app.services.locations import region_from_coordinates
from app.services.origin_search import OriginSearchService
from app.services.parking_nearby import ParkingNearbyService
from app.services.place_queries import PlaceQueryService
from app.services.query_cursors import QueryCursorStore
from app.services.recommendation_geography import RecommendationGeographyService


def source(provider, ref, collected_at, *, checked_at=None):
    return SourceReference(
        source_id=f"{provider}:{ref}",
        provider=provider,
        record_ref=ref,
        collected_at=collected_at,
        checked_at=checked_at,
    )


class DialogueTools:
    def __init__(self, readers, http, settings, *, driving_requests):
        self.readers = readers
        self.road_provider = SeoulAreaExtras(
            http, settings.seoul_citydata_api_key.get_secret_value()
        )
        key = settings.kakao_rest_api_key.get_secret_value()
        self.kakao = KakaoLocalClient(http, key)
        self.queries = PlaceQueryService(
            KakaoQueryClient(self.kakao),
            cursors=QueryCursorStore(),
            driving_requests=driving_requests,
            cache_namespace=hashlib.sha256(key.strip().encode()).hexdigest(),
        )
        self.origins = OriginSearchService(self.kakao)

    async def search_origins(self, query):
        async with asyncio.timeout(10):
            return await self.origins.search(query)

    async def search_places(self, query, collection_id):
        async with asyncio.timeout(15):
            found = await self.queries.search(query, limit=5)
            refs, failed = [], False
            for place in found.items:
                try:
                    region = region_from_coordinates(
                        await self.kakao.region(
                            place.coordinates.latitude, place.coordinates.longitude
                        )
                    )
                    if region != "seoul":
                        continue
                    refs.append(
                        PlaceReference(
                            ref_id=f"search:{collection_id}:{len(refs) + 1}",
                            collection_id=collection_id,
                            position=len(refs) + 1,
                            place=place,
                        )
                    )
                except ServiceError, KakaoError:
                    failed = True
            return LookupResult(
                outcome="partial"
                if refs and failed
                else "error"
                if failed
                else "ok"
                if refs
                else "empty",
                places=refs,
                has_more=found.has_more or failed,
                reason_code="REGION_LOOKUP_FAILED"
                if failed
                else "NO_MATCHES_IN_RETURNED_PAGE"
                if not refs
                else None,
            )

    async def read(self, topics, targets, context, *, completed=()):
        items = [i for i in completed if i.status != "error" or not i.retryable]
        for target in targets:
            for topic in dict.fromkeys(topics):
                if any(i.topic == topic and i.place_ref == target.ref_id for i in items):
                    continue
                await checkpoint("looking_up")
                try:
                    async with asyncio.timeout(10 if topic == "driving" else 5):
                        items.append(await self._read_one(topic, target, context))
                except (ServiceError, KakaoError, SQLAlchemyError, TimeoutError) as exc:
                    code = (
                        exc.code
                        if isinstance(exc, ServiceError)
                        else "PROVIDER_TIMEOUT"
                        if isinstance(exc, TimeoutError)
                        else "DATA_UNAVAILABLE"
                    )
                    items.append(
                        ReadItem(
                            topic=topic,
                            place_ref=target.ref_id,
                            status="error",
                            reason_code=code,
                            retryable=bool(getattr(exc, "retryable", True)),
                        )
                    )
                await checkpoint(
                    "looking_up", update={"items": [i.model_dump(mode="json") for i in items]}
                )
        statuses = {i.status for i in items}
        outcome = (
            "error"
            if statuses == {"error"}
            else "partial"
            if "error" in statuses
            else "unavailable"
            if statuses == {"unavailable"}
            else "empty"
            if statuses == {"empty"}
            else "partial"
            if "unavailable" in statuses
            else "ok"
        )
        return LookupResult(outcome=outcome, items=items)

    async def _read_one(self, topic, target, context):
        place = target.place
        common = {"topic": topic, "place_ref": target.ref_id}
        if topic == "place_info":
            return ReadItem(
                **common,
                status="ok",
                data=place.model_dump(mode="json", exclude={"coordinates"}),
                sources=[source("kakao_local", place.place_id, place.collected_at)],
            )
        if topic == "driving":
            origin = context.current_conditions.origin
            if origin is None or not origin.supported:
                return ReadItem(**common, status="unavailable", reason_code="ORIGIN_REQUIRED")
            data = await self.queries.driving(
                origin.coordinates, place.coordinates, include_geometry=False
            )
            return ReadItem(
                **common,
                status="ok",
                data={"origin_label": origin.label, **data.model_dump(mode="json")},
                sources=[
                    source(
                        "kakao_mobility",
                        place.place_id,
                        data.route.collected_at,
                        checked_at=data.checked_at,
                    )
                ],
            )
        if topic == "parking":
            async with self.readers() as db:
                parking = (
                    await ParkingNearbyService(
                        ParkingNearbyRepository(db), self.kakao, cursors=QueryCursorStore()
                    ).search(place.coordinates, 5, None, verified_seoul=True)
                ).parking
            parking.next_cursor = None
            status = (
                "ok"
                if parking.status == "available"
                else "empty"
                if parking.total_count == 0
                else "unavailable"
            )
            return ReadItem(
                **common,
                status=status,
                data=parking.model_dump(mode="json"),
                reason_code=parking.reason_code,
                sources=[
                    source(
                        "registered_parking", place.place_id, None, checked_at=parking.checked_at
                    )
                ],
            )
        if topic == "traffic":
            async with self.readers() as db:
                road = await RecommendationGeographyService(
                    RecommendationGeographyRepository(db), self.road_provider
                ).traffic(place.coordinates)
            return ReadItem(
                **common,
                status="ok" if road.grade else "unavailable",
                data={
                    "grade": road.grade,
                    "radius_meters": 100,
                    "scope": "destination_surroundings",
                },
                reason_code=road.reason,
                sources=[source(road.source or "seoul_citydata", place.place_id, road.observed_at)],
            )
        if target.result_id is None:
            return ReadItem(**common, status="unavailable", reason_code="NOT_A_RECOMMENDATION")
        async with self.readers() as db:
            owned = await owned_recommendation(
                db, context.identity.conversation_id, target.result_id
            )
        if owned is None:
            return ReadItem(**common, status="unavailable", reason_code="EVIDENCE_NOT_RECORDED")
        card = next(
            (c for c in owned.recommendation.cards if c.place.value.place_id == place.place_id),
            None,
        )
        if card is None:
            return ReadItem(**common, status="unavailable", reason_code="EVIDENCE_NOT_RECORDED")
        prepared = owned.snapshot.finalization.prepared
        return ReadItem(
            **common,
            status="ok",
            data={
                "selection_mode": prepared.selection_mode,
                "search_area": owned.recommendation.search_area.model_dump(mode="json")
                if owned.recommendation.search_area
                else None,
                "search_scope": prepared.search_scope.model_dump(mode="json")
                if prepared.search_scope
                else None,
                "result_id": str(target.result_id),
                "recommended_at": prepared.prepared_at.isoformat(),
                "conditions": prepared.plan.effective_conditions.model_dump(mode="json"),
                "reason": card.reason.model_dump(mode="json"),
                "rank": card.rank,
                "ranking": card.ranking.model_dump(mode="json") if card.ranking else None,
                "ranking_policy": owned.snapshot.finalization.prepared.ranking_policy.model_dump(
                    mode="json"
                )
                if owned.snapshot.finalization.prepared.ranking_policy
                else None,
            },
            sources=[
                source("recommendation_snapshot", str(target.result_id), prepared.prepared_at)
            ],
        )


def explain_lookup(lookup):
    """Render verified facts; no extra model call or fabricated source/availability."""
    if lookup.places:
        return (
            "검색한 장소예요: "
            + ", ".join(f"{r.position}. {r.place.name}" for r in lookup.places)
            + ". 추천 순위가 아닌 검색 목록이에요."
        )
    if not lookup.items:
        return (
            "장소 조회에 실패했어요."
            if lookup.outcome == "error"
            else "조회한 범위에서 서울 장소를 찾지 못했어요."
        )
    parts = []
    for item in lookup.items:
        if item.status == "error":
            parts.append(
                {
                    "parking": "주차",
                    "driving": "이동시간",
                    "recommendation_reason": "당시 근거",
                    "place_info": "장소 정보",
                    "traffic": "주변 도로",
                }[item.topic]
                + " 조회에 실패했어요."
            )
        elif item.status == "unavailable":
            parts.append(
                "현재 출발지가 필요해 이동시간을 조회하지 못했어요."
                if item.reason_code == "ORIGIN_REQUIRED"
                else "해당 정보의 확인된 자료가 없어요."
            )
        elif item.topic == "place_info":
            data = item.data
            address = data.get("road_address") or data.get("address") or "미확인"
            parts.append(
                f"{data['name']}: 주소 {address}, "
                f"전화 {data.get('phone') or '미확인'}. 검색·추천 당시 확인한 기본정보예요."
            )
        elif item.topic == "driving":
            seconds = item.data["route"]["value"]["duration_seconds"]
            parts.append(
                f"현재 출발지 {item.data['origin_label']}에서 "
                f"자동차로 약 {round(seconds / 60)}분이에요."
            )
        elif item.topic == "parking":
            parts.append(
                f"직선거리 {item.data['search_radius_meters']}m 범위에 "
                f"등록 주차장 {item.data['total_count']}곳이 확인됐어요."
            )
        elif item.topic == "traffic":
            parts.append(
                f"도착지 주변 100m에서 확인한 도로 등급은 {item.data['grade']}예요. "
                "전체 주행 경로의 정체를 뜻하지 않아요."
            )
        else:
            parts.append(
                "당시 추천 근거: "
                + item.data["reason"]["text"]
                + " 현재 상태를 다시 조회한 값은 아니에요."
            )
    return " ".join(parts)
