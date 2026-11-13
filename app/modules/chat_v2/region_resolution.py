"""Bounded destination resolution: catalog, verified geometry, then Kakao identity."""

import asyncio
import re
from datetime import UTC, datetime

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from app.clients.kakao import KakaoError
from app.core.exceptions import ServiceError
from app.modules.chat_v2.control import checkpoint
from app.modules.chat_v2.destination import resolve_destination
from app.modules.chat_v2.region_aliases import ALIASES, VERSION
from app.modules.chat_v2.region_contracts import (
    DestinationArea,
    DestinationOption,
    RegionAnchor,
    RegionResolution,
)
from app.modules.chat_v2.region_repository import official_areas, seoul_shape
from app.schemas.location import Coordinates
from app.services.locations import documents


def normalized(value):
    return re.sub(r"\s+|[()]", "", value.replace("서울특별시", "").replace("서울시", ""))


def radius_region(anchor, query, radius):
    return DestinationArea(
        kind="radius",
        query=query,
        source="kakao_local",
        anchor=anchor,
        radius_m=radius,
        label=f"{anchor.name} 반경 {radius:,}m · 서울 내",
    )


def options_result(regions, descriptions):
    return RegionResolution(
        status="ambiguous",
        message="어느 곳을 기준으로 찾을까요? 다른 이름이나 주소를 적어도 돼요.",
        options=[
            DestinationOption(position=i, region=r, description=d)
            for i, (r, d) in enumerate(zip(regions[:3], descriptions[:3], strict=True), 1)
        ],
    )


async def resolve_region(readers, kakao, query, *, relation="area", radius=None, current=None):
    if radius is not None and not 100 <= radius <= 5000:
        return RegionResolution(
            status="needs_input",
            message="주변 검색은 직선거리 100m~5km로 할 수 있어요. 어느 거리로 찾을까요?",
        )
    if relation == "inside":
        return RegionResolution(
            status="needs_input",
            message=(
                "시설 내부를 구분하는 경계 자료는 아직 없어요. "
                "내부 대신 주변에서 찾으려면 ‘주변’이라고 알려주세요."
            ),
        )
    if not query:
        if current and current.anchor:
            return RegionResolution(
                status="resolved",
                region=radius_region(current.anchor, current.query, radius or 1000),
            )
        if current and current.kind == "official" and (current.query or "").endswith("역"):
            query, relation = current.query, "near"
        else:
            return RegionResolution(
                status="needs_input", message="반경 검색의 중심으로 삼을 역이나 장소를 알려주세요."
            )
    # The model supplies relations, but explicit text must never be swallowed by an alias.
    if re.search(r"(?:근처|주변)\s*$", query):
        relation = "near"
    clean = re.sub(r"\s*(?:근처|주변|일대)\s*$", "", query).strip()
    try:
        async with asyncio.timeout(10):
            await checkpoint("searching")
            async with readers() as db:
                seoul = await seoul_shape(db)
                official = await official_areas(db) if relation == "area" and radius is None else []
            if seoul is None:
                raise ServiceError("DATA_UNAVAILABLE", "서울 경계가 준비되지 않았어요.")
            if relation == "area" and radius is None:
                key = normalized(clean)
                alias = ALIASES.get(key)
                matched = (
                    [a for a in official if (a.area_id, a.name) == alias]
                    if alias
                    else [a for a in official if normalized(a.name) == key]
                )
                if alias and not matched:
                    return RegionResolution(
                        status="unavailable",
                        message=(
                            "이 지역의 경계 자료를 지금 확인하지 못했어요. "
                            "잠시 후 다시 시도해 주세요."
                        ),
                    )
                if matched:
                    regions = [
                        DestinationArea(
                            kind="official",
                            query=query,
                            label=f"{a.name} 기준",
                            boundary_ids=[a.area_id],
                            boundary_versions={a.area_id: a.geometry_version},
                            source=a.source,
                            alias_version=VERSION if alias else None,
                        )
                        for a in matched
                    ]
                    if not all(seoul.contains(a.center) for a in matched):
                        return RegionResolution(
                            status="out_of_scope",
                            message="현재는 서울 안의 장소를 추천하고 있어요.",
                        )
                    return (
                        RegionResolution(status="resolved", region=regions[0])
                        if len(regions) == 1
                        else options_result(regions, ["서울 공식 관심지역 경계" for _ in regions])
                    )
                admin = await resolve_destination(readers, clean)
                if admin:
                    admin.query, admin.source = query, "sgis_areas"
                    return RegionResolution(status="resolved", region=admin)
            return await _external(kakao, clean, radius or 1000, seoul)
    except ServiceError, KakaoError, TimeoutError, SQLAlchemyError:
        return RegionResolution(
            status="unavailable",
            message="지역 위치 조회에 실패했어요. 잠시 후 같은 요청을 다시 보내주세요.",
        )


async def _external(kakao, query, radius, seoul):
    found, invalid, ended = {}, False, False
    for page in (1, 2):
        await checkpoint("searching")
        rows, ended = documents(await kakao.keyword(query, page=page, size=15))
        for row in rows:
            try:
                if not row.get("id") or not row.get("place_name"):
                    raise ValueError("missing identity")
                anchor = RegionAnchor(
                    place_id="kakao:" + row["id"],
                    name=row["place_name"],
                    coordinates=Coordinates(latitude=row["y"], longitude=row["x"]),
                    address=row.get("road_address_name")
                    or row.get("address_name")
                    or "주소 미제공",
                    category=row.get("category_name") or "분류 미제공",
                    collected_at=datetime.now(UTC),
                )
                # A station request must not silently select a shop named after that station.
                if normalized(query).endswith("역") and row.get("category_group_code") != "SW8":
                    continue
                gate = re.search(r"(정문|후문)$", normalized(query))
                if gate:
                    # A facility entrance request must not offer nearby businesses named after it.
                    name = normalized(anchor.name)
                    base = normalized(query)[: gate.start()]
                    if not name.endswith(gate[1]) or (
                        "대학교" in base and not name.startswith(base)
                    ):
                        continue
                found[anchor.place_id] = anchor
            except KeyError, ValueError, TypeError, ValidationError:
                invalid = True
        if ended:
            break
    exact = [a for a in found.values() if normalized(a.name) == normalized(query)]
    candidates = exact or list(found.values())
    local = [a for a in candidates if seoul.contains(a.coordinates)]
    if not candidates:
        return RegionResolution(
            status="unavailable" if invalid else "not_found",
            message="장소 검색 결과를 확인하지 못했어요. 다시 시도해 주세요."
            if invalid
            else "그 장소를 찾지 못했어요. 다른 이름이나 가까운 역·주소를 알려주세요.",
        )
    if not local:
        return RegionResolution(
            status="out_of_scope",
            message="찾은 장소는 서울 밖에 있어요. 서울 안의 지역이나 장소를 알려주세요.",
        )
    regions = [radius_region(a, query, radius) for a in local]
    # Unique exact, complete provider evidence only; a fuzzy first hit always needs confirmation.
    if len(candidates) == 1 and exact and ended and not invalid:
        return RegionResolution(status="resolved", region=regions[0])
    return options_result(regions, [f"{a.address} · {a.category}" for a in local])
