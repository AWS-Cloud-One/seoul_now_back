"""Authenticated historical place identity plus explicitly requested current facts."""

import hashlib
import json
from datetime import UTC, datetime

from sqlalchemy.exc import SQLAlchemyError

from app.core.exceptions import ServiceError
from app.modules.recommendation.cards import unavailable_parking
from app.schemas.common import Information
from app.schemas.pending import Place, PlaceDetailData, RouteValue, TravelTimeData
from app.services.driving_requests import ETA_TTL_SECONDS


def selected_place(stored, place_id):
    for card, coordinates in zip(stored.cards, stored.card_coordinates, strict=True):
        if card.place_id == place_id:
            return card, Place(
                place_id=card.place_id,
                name=card.place_name,
                purpose=card.purpose,
                category_code=card.category_code,
                category_name=card.category_name,
                coordinates=coordinates,
                address=card.address,
                road_address=card.road_address,
                phone=card.phone,
                provider_url=card.provider_url,
            )
    raise ServiceError("INVALID_CONTEXT", "추천 결과의 장소를 확인할 수 없습니다.", retryable=False)


def recommended_at(stored):
    return stored.result.created_at if stored.result.kind == "recommendation" else None


class PlaceDetailReader:
    def __init__(self, contexts, parking, *, clock=lambda: datetime.now(UTC)):
        self.contexts, self.parking, self.clock = contexts, parking, clock

    async def detail(self, place_id, context_token):
        stored = await self.contexts.resolve(context_token, place_id=place_id)
        card, place = selected_place(stored, place_id)
        try:
            parking = (
                await self.parking.search(
                    place.coordinates,
                    limit=20,
                    cursor=None,
                    verified_seoul=True,
                )
            ).parking
        except ServiceError, SQLAlchemyError:
            parking = unavailable_parking("DATA_UNAVAILABLE", error=True)
        self.contexts.verify_loaded(stored, context_token, place_id=place_id)
        return PlaceDetailData(
            result_id=str(stored.result.result_id),
            place=Information[Place](
                value=place,
                status="available",
                **{
                    field: getattr(card, "place_" + field)
                    for field in (
                        "freshness",
                        "observed_at",
                        "collected_at",
                        "source",
                        "reason_code",
                    )
                },
            ),
            parking=parking,
            original_recommended_at=recommended_at(stored),
            checked_at=self.clock(),
        )


class TravelTimeReader:
    def __init__(self, contexts, conditions, queries, replay, *, clock=lambda: datetime.now(UTC)):
        self.contexts, self.conditions = contexts, conditions
        self.queries, self.replay, self.clock = queries, replay, clock

    async def check(self, place_id, request):
        stored = await self.contexts.resolve(
            request.context_token,
            trip_id=request.trip_id,
            result_id=request.result_id,
            place_id=place_id,
        )
        try:
            current = await self.conditions.get_current(request.trip_id)
        except SQLAlchemyError:
            raise ServiceError("DATA_UNAVAILABLE", "현재 요청 버전을 확인할 수 없습니다.") from None
        # The UI version must be current; the selected result may belong to older conditions.
        if current is None or current.version != request.condition_version:
            raise ServiceError(
                "CONTEXT_CONDITIONS_MISMATCH",
                "현재 여행의 조건 버전을 확인해 주세요.",
                retryable=False,
            )
        card, place = selected_place(stored, place_id)
        if stored.conditions.origin is None:
            raise ServiceError(
                "INVALID_CONTEXT", "원래 출발지를 확인할 수 없습니다.", retryable=False
            )

        def verify():
            self.contexts.verify_loaded(
                stored,
                request.context_token,
                trip_id=request.trip_id,
                result_id=request.result_id,
                place_id=place_id,
            )

        async def compute():
            origin, destination = stored.conditions.origin.coordinates, place.coordinates
            at = card.route_collected_at
            if (
                card.route_status == "available"
                and card.route_source == "kakao_mobility"
                and at is not None
                and 0 <= (self.clock() - at).total_seconds() < ETA_TTL_SECONDS
                and card.duration_seconds is not None
                and card.distance_meters is not None
            ):
                self.queries.remember_route(
                    origin,
                    destination,
                    Information[RouteValue](
                        value={
                            "duration_seconds": card.duration_seconds,
                            "distance_meters": card.distance_meters,
                        },
                        status="available",
                        freshness="fresh",
                        observed_at=card.route_observed_at,
                        collected_at=at,
                        source=card.route_source,
                        reason_code=None,
                    ),
                )
            try:
                data = await self.queries.driving(origin, destination, include_geometry=False)
            finally:
                verify()
            return TravelTimeData(
                trip_id=request.trip_id,
                base_condition_version=request.condition_version,
                result_id=request.result_id,
                place_id=place_id,
                original_recommended_at=recommended_at(stored),
                checked_at=self.clock(),
                route=Information[RouteValue](
                    **data.route.model_dump(exclude={"value"}),
                    value={
                        "duration_seconds": data.route.value.duration_seconds,
                        "distance_meters": data.route.value.distance_meters,
                    },
                ),
            )

        signature = hashlib.sha256(
            json.dumps(
                {
                    "operation": "travel_time",
                    "place_id": place_id,
                    **request.model_dump(mode="json"),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        result = await self.replay.run((request.trip_id, request.request_id), signature, compute)
        verify()  # Replays and waiters also verify expiry after any waiting.
        return result
