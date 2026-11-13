"""Adapt the shared Kakao and parking services to the recommendation ports."""

from datetime import UTC, datetime

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from app.clients.kakao import KakaoError
from app.core.exceptions import ServiceError
from app.modules.recommendation.types import Candidate, RouteEstimate
from app.schemas.location import Coordinates
from app.schemas.pending import Place
from app.schemas.recommendations import Conditions, ConditionUpdate
from app.services.locations import documents, provider_failure, region_from_coordinates, text
from app.services.recommendations import ValidationDecision


class RecommendationSource:
    def __init__(self, kakao, parking, queries):
        self.kakao, self.parking_service = kakao, parking
        self.queries = queries

    async def region(self, coordinates):
        try:
            return region_from_coordinates(
                await self.kakao.region(coordinates.latitude, coordinates.longitude)
            )
        except KakaoError as exc:
            raise provider_failure(exc) from None

    async def search(self, query, origin, purpose, page, *, radius=None, rect=None):
        try:
            rows, is_end = documents(
                await self.kakao.nearby_keyword(
                    query,
                    origin,
                    page=page,
                    **({"radius": radius} if radius is not None else {}),
                    **({"rect": rect} if rect is not None else {}),
                )
            )
        except KakaoError as exc:
            raise provider_failure(exc) from None
        candidates = []
        now = datetime.now(UTC)
        for row in rows:
            try:
                identifier, name = text(row.get("id")), text(row.get("place_name"))
                if not identifier or not name:
                    raise ValueError("missing identity")
                candidates.append(
                    Candidate(
                        place=Place(
                            place_id="kakao:" + identifier,
                            name=name,
                            purpose=purpose,
                            category_code=text(row.get("category_group_code")),
                            category_name=text(row.get("category_name")),
                            coordinates=Coordinates(latitude=row["y"], longitude=row["x"]),
                            address=text(row.get("address_name")),
                            road_address=text(row.get("road_address_name")),
                            phone=text(row.get("phone")),
                            provider_url=text(row.get("place_url")),
                        ),
                        collected_at=now,
                    )
                )
            except ValueError, TypeError, KeyError, ValidationError:
                continue
        if rows and not candidates:
            raise ServiceError("PROVIDER_ERROR", "장소 검색 결과를 검증할 수 없습니다.")
        return candidates, is_end

    async def route(self, origin, destination):
        data = await self.queries.driving(origin, destination, include_geometry=False)
        return RouteEstimate(
            data.route.value.duration_seconds,
            data.route.value.distance_meters,
            data.route.collected_at,
        )

    async def parking(self, coordinates):
        # Use the published, normalized facilities and the same first-radius rule as P02.
        try:
            return (await self.parking_service.search(coordinates, limit=100, cursor=None)).parking
        except SQLAlchemyError:
            raise ServiceError("DATA_UNAVAILABLE", "등록 주차정보를 조회할 수 없습니다.") from None


class RecommendationConditionsValidator:
    def __init__(self, source):
        self.source = source

    async def validate(self, request):
        values = request.current_conditions.model_dump(mode="json")
        values.update(request.conditions_patch.model_dump(mode="json", exclude_unset=True))
        conditions = Conditions.model_validate(values)
        if conditions.origin is None or not conditions.purposes:
            raise ServiceError(
                "VALIDATION_ERROR", "출발지와 추천 목적이 필요합니다.", retryable=False
            )
        purposes = [item.purpose for item in conditions.purposes]
        if len(set(purposes)) != len(purposes):
            raise ServiceError(
                "VALIDATION_ERROR", "추천 목적을 중복 지정할 수 없습니다.", retryable=False
            )
        if conditions.age_groups is not None and (
            not conditions.age_groups
            or len(set(conditions.age_groups)) != len(conditions.age_groups)
        ):
            raise ServiceError(
                "VALIDATION_ERROR", "연령대는 중복 없이 지정해 주세요.", retryable=False
            )
        region = await self.source.region(conditions.origin.coordinates)
        if region not in {"seoul", "incheon", "gyeonggi"}:
            raise ServiceError(
                "ORIGIN_OUT_OF_SERVICE_AREA",
                "서울·인천·경기 출발지를 선택해 주세요.",
                retryable=False,
            )
        conditions.origin.region, conditions.origin.supported = region, True
        before, after = (
            request.current_conditions.model_dump(mode="json"),
            conditions.model_dump(mode="json"),
        )
        changes = [
            {"field": field, "before": before[field], "after": value}
            for field, value in after.items()
            if before[field] != value
        ]
        return ValidationDecision(
            ConditionUpdate(
                applied=True,
                conditions=conditions,
                version=request.condition_version + bool(changes),
                changes=changes,
            )
        )
