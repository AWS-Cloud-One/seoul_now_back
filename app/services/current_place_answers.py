"""Deterministic current-fact answers. The model selects intent, never invents facts."""

from zoneinfo import ZoneInfo

from app.schemas.common import Information
from app.schemas.pending import Answer, TravelTimeRequest
from app.services.area_queries import missing
from app.services.place_details import recommended_at, selected_place

CONGESTION_LABELS = {
    "relaxed": "여유",
    "normal": "보통",
    "slightly_crowded": "약간 붐빔",
    "crowded": "붐빔",
}


def population_sentence(area):
    info = area["population"]
    prefix = f"{area['name']}({area['area_code']})"
    if info["status"] != "available":
        reason = {
            "STALE_DATA": "관측이 오래되어 현재 인구를 확인할 수 없습니다",
            "SOURCE_TIME_UNKNOWN": "관측 시각을 확인할 수 없습니다",
            "REPLACEMENT_VALUE": "대체값이어서 현재 관측 인구로 안내할 수 없습니다",
            "GEOMETRY_VERSION_MISMATCH": "관측 경계 버전이 일치하지 않습니다",
            "COLLECTION_FAILED": "수집 실패로 인구를 확인할 수 없습니다",
        }.get(info["reason_code"], "확인 가능한 인구 관측 자료가 없습니다")
        return f"{prefix}: {reason}."
    value = info["value"]
    low, high = value.get("min_people"), value.get("max_people")
    count = (
        f"관측 인구 {low:,}~{high:,}명"
        if low is not None and high is not None
        else "인원 범위 미확인"
    )
    grade = CONGESTION_LABELS.get(value.get("congestion"), "미확인")
    at = info["observed_at"].astimezone(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M")
    caveat = " 오래된 관측입니다." if info["freshness"] == "stale" else ""
    if info["reason_code"] == "COLLECTION_FAILED":
        caveat += " 최근 수집에 실패해 보관된 관측을 표시합니다."
    return f"{prefix}: {at} 한국시간 관측 기준 {count}, 혼잡도 {grade}.{caveat}"


class CurrentPlaceAnswers:
    def __init__(self, places, travel, areas):
        self.places, self.travel, self.areas = places, travel, areas

    async def answer(self, request, topic, stored):
        target = request.target
        if topic == "travel_time":
            data = await self.travel.check(
                target.place_id,
                TravelTimeRequest(
                    request_id=request.request_id,
                    trip_id=request.trip_id,
                    condition_version=request.condition_version,
                    result_id=target.result_id,
                    context_token=target.context_token,
                ),
            )
            seconds = data.route.value.duration_seconds
            message = (
                "이 결과의 원래 출발지 기준 자동차 이동시간은 "
                f"{seconds // 60}분 {seconds % 60}초입니다. "
                "정상 조회 후 60초 이내의 값은 재사용합니다."
            )
            checked_at, facts = data.checked_at, [data.route.model_dump(mode="json")]
        elif topic == "parking":
            data = await self.places.detail(target.place_id, target.context_token)
            parking = data.parking
            if parking.status == "available":
                message = (
                    f"직선거리 {parking.search_radius_meters}m 범위에서 등록 주차장 "
                    f"{parking.total_count}곳을 확인했습니다."
                )
                if parking.nearest_distance_meters is not None:
                    message += (
                        " 가장 가까운 등록 주차장까지 "
                        f"약 {parking.nearest_distance_meters:g}m입니다."
                    )
            elif parking.status == "error":
                message = "주차 등록정보 조회에 실패했습니다."
            elif parking.total_count == 0:
                message = (
                    f"보유 자료에서 직선거리 {parking.search_radius_meters}m 내 "
                    "등록 주차장을 찾지 못했습니다."
                )
            else:
                message = "주차 등록정보가 준비되지 않아 확인할 수 없습니다."
            message += " 실시간 빈자리는 제공하지 않습니다."
            checked_at = parking.checked_at
            facts = [
                dict(
                    value={
                        "scope": "registered_parking",
                        "availability": "unsupported",
                        **parking.model_dump(
                            mode="json", exclude={"status", "reason_code", "checked_at"}
                        ),
                    },
                    status=parking.status,
                    freshness="unknown",
                    observed_at=None,
                    collected_at=None,
                    source=None,
                    reason_code=parking.reason_code,
                )
            ]
        elif topic == "population":
            _, place = selected_place(stored, target.place_id)
            data = await self.areas.population_at(place.coordinates)
            checked_at, facts = data["checked_at"], []
            for area in data["areas"]:
                info = area["population"]
                facts.append(
                    info
                    | {
                        "value": {
                            "area_code": area["area_code"],
                            "name": area["name"],
                            "boundary_version": area["boundary_version"],
                            "scope": "population_observation_area",
                            "population": info["value"],
                        }
                    }
                )
            if data["areas"]:
                message = " ".join(population_sentence(area) for area in data["areas"])
                message += (
                    " 각 관측 구역 전체의 자료이며, 매장 내부 혼잡도나 소음을 뜻하지 않습니다."
                )
            else:
                message = (
                    "선택한 장소가 보유한 인구 관측 구역 밖에 있어 인구를 확인할 수 없습니다."
                    if data["reason_code"] == "OUTSIDE_OBSERVATION_AREA"
                    else "인구 관측 경계 자료가 준비되지 않아 확인할 수 없습니다."
                )
                facts = [missing(data["reason_code"])]
        else:
            raise ValueError("Unsupported current-fact topic")
        return Answer(
            topic=topic,
            place_id=target.place_id,
            text=message,
            original_result_id=target.result_id,
            original_recommended_at=recommended_at(stored),
            checked_at=checked_at,
            information=[Information.model_validate(fact) for fact in facts],
        )
