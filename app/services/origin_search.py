"""Bounded origin lookup and ranked confirmation candidates; never applies conditions."""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from app.clients.kakao import KakaoError, KakaoLocalClient
from app.core.exceptions import ServiceError
from app.schemas.location import Coordinates, Location
from app.schemas.origin import OriginCandidate, OriginChoiceValue, OriginClarificationContext
from app.schemas.recommendations import Clarification, ConditionsPatch, WarningMessage
from app.services.locations import (
    documents,
    provider_error,
    provider_failure,
    region_from_coordinates,
    text,
)
from app.services.origin_matching import (
    OriginSearchIntent,
    address_text,
    assess,
    effective_hints,
    match_reasons,
    order_key,
    province,
    semantic_key,
)

SEARCH_SIZE = 15
MAX_REGION_LOOKUPS = 5
MAX_CHOICES = 5


@dataclass(frozen=True)
class OriginSearchResult:
    candidates: tuple[OriginCandidate, ...]
    unsupported_candidates: tuple[OriginCandidate, ...]
    mode: Literal["confirm", "choose", "refine"]
    search_limited: bool
    warnings: tuple[WarningMessage, ...]
    reason_code: str | None = None

    def clarification(
        self, *, context_token: str, expires_at: datetime, pending_patch: ConditionsPatch
    ):
        """The conversation owner issues/stores the token; this only formats its display."""
        preferred = self.candidates[0] if self.mode == "confirm" else None
        if preferred:
            location = preferred.location
            question = f"{location.label} · {location.address or '주소 미확인'}에서 출발하시나요?"
        elif self.mode == "choose":
            question = "말씀하신 출발지에 해당하는 장소를 선택해 주세요."
        elif self.reason_code == "ORIGIN_OUT_OF_SERVICE_AREA":
            question = "확인한 출발지는 지원 범위 밖입니다. 서울·인천·경기 출발지를 입력해 주세요."
        elif self.reason_code == "ORIGIN_HINTS_CONFLICT":
            question = "출발지의 지역이나 장소 종류를 하나로 확인해 주세요."
        else:
            question = (
                "조회한 범위에서 출발지를 확인하지 못했습니다. 지역이나 주소를 더 알려 주세요."
            )
        return Clarification(
            question=question,
            required_fields=["origin"],
            pending_patch=pending_patch.model_copy(deep=True),
            choices=[
                {
                    "label": f"{c.location.label} · {c.location.address or '주소 미확인'}",
                    "value": OriginChoiceValue(
                        candidate_id=c.candidate_id,
                        location=c.location.model_copy(deep=True),
                        match_reasons=match_reasons(c),
                    ).model_dump(mode="json"),
                }
                for c in self.candidates
            ],
            origin_context=OriginClarificationContext(
                context_token=context_token,
                expires_at=expires_at,
                mode=self.mode,
                preferred_candidate_id=preferred.candidate_id if preferred else None,
                search_limited=self.search_limited,
            ),
        )


def candidate_from_row(row, source, position):
    coordinates = Coordinates(latitude=row["y"], longitude=row["x"])
    road = row.get("road_address") if isinstance(row.get("road_address"), dict) else {}
    land_address = text(row.get("address_name"))
    road_address = text(row.get("road_address_name")) or text(road.get("address_name"))
    provider_id = text(row.get("id")) if source == "kakao_keyword" else None
    label = (
        text(row.get("place_name")) if source == "kakao_keyword" else land_address or road_address
    )
    if label is None or (source == "kakao_keyword" and provider_id is None):
        raise ValueError("missing provider identity")
    identity = (
        (source, provider_id)
        if provider_id
        else (source, address_text(label), coordinates.latitude, coordinates.longitude)
    )
    identifier = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()[:24]
    return OriginCandidate(
        candidate_id="origin-" + identifier,
        source=source,
        provider_id=provider_id,
        location=Location(
            label=label,
            coordinates=coordinates,
            address=road_address or land_address,
            region=None,
            supported=False,
        ),
        land_address=land_address,
        road_address=road_address,
        category_code=text(row.get("category_group_code")),
        category_name=text(row.get("category_name")),
        provider_order=position,
    )


def unique_candidates(candidates):
    unique, conflicts = {}, set()
    for candidate in candidates:
        previous = unique.get(candidate.candidate_id)
        if previous is not None and previous.location.coordinates != candidate.location.coordinates:
            conflicts.add(candidate.candidate_id)
        elif previous is None:
            unique[candidate.candidate_id] = candidate
    return [c for key, c in unique.items() if key not in conflicts], bool(conflicts)


class OriginSearchService:
    def __init__(self, provider: KakaoLocalClient):
        self.provider = provider

    async def _page(self, query, *, address=False, category=None, offset=0):
        try:
            if address:
                body = await self.provider.address(query, size=SEARCH_SIZE)
            else:
                options = {"category": category} if category else {}
                body = await self.provider.keyword(query, page=1, size=SEARCH_SIZE, **options)
            rows, ended = documents(body)
            if len(rows) > SEARCH_SIZE:
                raise provider_error()
            values, invalid = [], False
            for position, row in enumerate(rows, offset):
                try:
                    values.append(
                        candidate_from_row(
                            row, "kakao_address" if address else "kakao_keyword", position
                        )
                    )
                except ValueError, TypeError, KeyError:
                    invalid = True
            if rows and not values:
                raise provider_error()
            return values, not ended, invalid
        except KakaoError as exc:
            raise provider_failure(exc) from None

    async def search(self, name: str, hints=()) -> OriginSearchResult:
        if not isinstance(name, str) or not name.strip() or len(name) > 100:
            raise ServiceError("VALIDATION_ERROR", "출발지 이름은 1~100자로 입력해 주세요.")
        name = name.strip()
        intent = OriginSearchIntent(name, effective_hints(name, hints))
        if intent.conflicting:
            return OriginSearchResult((), (), "refine", False, (), "ORIGIN_HINTS_CONFLICT")
        query = intent.query()
        if len(query) > 200:
            raise ServiceError(
                "VALIDATION_ERROR", "출발지의 지역·주소를 더 간결하게 입력해 주세요."
            )
        warnings, errors = {}, []

        def warn(code, message):
            warnings[code] = WarningMessage(code=code, message=message)

        values, limited, invalid = await self._page(query)
        eligible = [assess(c, intent) for c in values]
        eligible = [c for c in eligible if not c.match.conflicts]
        fallback = None
        if not eligible:
            if intent.is_address:
                fallback = (intent.address_query(), True, None)
            elif query != name:
                fallback = (name, False, intent.category)
            elif intent.category and values:
                fallback = (query, False, intent.category)
            elif intent.category is None:
                fallback = (query, True, None)
        elif intent.category and not any(c.match.place_type == "match" for c in eligible):
            fallback = (query, False, intent.category)
        if fallback is not None:
            try:
                extra, more, bad = await self._page(
                    fallback[0], address=fallback[1], category=fallback[2], offset=SEARCH_SIZE
                )
                values.extend(extra)
                limited, invalid = limited or more, invalid or bad
            except ServiceError as exc:
                if exc.code in {"RATE_LIMITED", "SERVICE_UNAVAILABLE"}:
                    # Keyword and coordinate-region requests share the Local API.
                    # Do not spend more calls after a known quota/auth failure.
                    raise
                errors.append(exc)
                limited = True
                warn(exc.code, "추가 출발지 검색에 실패해 먼저 확인한 결과만 사용합니다.")
        values, identity_conflict = unique_candidates(values)
        if identity_conflict and not values:
            raise provider_error()
        if invalid or identity_conflict:
            limited = True
            warn(
                "ORIGIN_SEARCH_PARTIAL",
                "일부 검색 결과의 필수 정보나 동일 장소의 좌표를 확인하지 못했습니다.",
            )
        ranked = sorted((assess(c, intent) for c in values), key=order_key)
        ranked = [c for c in ranked if not c.match.conflicts]
        verified, unsupported, regions = [], [], {}
        for candidate in ranked:
            point = candidate.location.coordinates
            key = (point.latitude, point.longitude)
            if key not in regions:
                if len(regions) >= MAX_REGION_LOOKUPS:
                    limited = True
                    warn(
                        "ORIGIN_SEARCH_LIMITED", "지역 확인 상한에 도달해 일부 후보만 확인했습니다."
                    )
                    continue
                try:
                    body = await self.provider.region(*key)
                    region = region_from_coordinates(body)
                    addresses = list(
                        dict.fromkeys(
                            " ".join(
                                filter(
                                    None,
                                    (
                                        text(row.get(f"region_{depth}depth_name"))
                                        for depth in (1, 2, 3)
                                    ),
                                )
                            )
                            for row in body["documents"]
                            if row.get("region_type") in {"B", "H"}
                        )
                    )
                    if len(addresses) > 2 or any(len(value) > 200 for value in addresses):
                        raise provider_error()
                    regions[key] = region, addresses
                except KakaoError as exc:
                    regions[key] = provider_failure(exc)
                except ServiceError as exc:
                    regions[key] = exc
            result = regions[key]
            if isinstance(result, ServiceError):
                errors.append(result)
                limited = True
                warn(result.code, "일부 출발지의 좌표 기반 지역 확인에 실패했습니다.")
                if result.code in {"RATE_LIMITED", "SERVICE_UNAVAILABLE"}:
                    break
                continue
            region, addresses = result
            stated = {
                province(a) for a in (candidate.land_address, candidate.road_address) if a
            } - {None}
            actual = {province(a) for a in addresses} - {None}
            if stated and actual and not stated <= actual:
                error = provider_error()
                errors.append(error)
                limited = True
                warn(
                    "ORIGIN_SEARCH_PARTIAL",
                    "검색 주소와 좌표의 지역이 일치하지 않는 후보를 제외했습니다.",
                )
                continue
            location = candidate.location.model_copy(
                update={"region": region, "supported": region in {"seoul", "incheon", "gyeonggi"}}
            )
            candidate = assess(
                candidate.model_copy(update={"location": location, "region_addresses": addresses}),
                intent,
            )
            if candidate.match.conflicts:
                continue
            (verified if location.supported else unsupported).append(candidate)
        if not verified and errors:
            raise errors[0]
        verified.sort(key=order_key)
        unsupported.sort(key=order_key)
        if unsupported:
            warn("ORIGIN_OUT_OF_SERVICE_AREA", "지원 범위 밖으로 확인된 출발지 후보가 있습니다.")
        # Never substitute a weaker in-scope place for a more specific unsupported one.
        if unsupported and verified and semantic_key(unsupported[0]) < semantic_key(verified[0]):
            verified = []
        limited = limited or len(verified) > MAX_CHOICES
        if limited:
            warn(
                "ORIGIN_SEARCH_LIMITED",
                "제한된 검색·확인 범위의 후보입니다. 다른 지역이나 주소로 다시 검색할 수 있습니다.",
            )
        shown = tuple(verified[:MAX_CHOICES])
        mode = (
            "refine"
            if not shown
            else (
                "choose"
                if len(shown) > 1 and semantic_key(shown[0]) == semantic_key(shown[1])
                else "confirm"
            )
        )
        reason = "ORIGIN_OUT_OF_SERVICE_AREA" if not shown and unsupported else None
        if not shown and reason is None:
            reason = "ORIGIN_SEARCH_INCOMPLETE" if limited else "ORIGIN_NOT_FOUND"
        return OriginSearchResult(
            shown, tuple(unsupported), mode, limited, tuple(warnings.values()), reason
        )
