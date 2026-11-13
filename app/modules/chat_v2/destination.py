"""Resolve a named destination against stored Seoul boundaries, never the origin."""

import re

from sqlalchemy import func, select

from app.core.exceptions import ServiceError
from app.models import AdministrativeArea, RegionBoundary
from app.modules.chat_v2.dialogue_context import DestinationArea
from app.repositories.recommendation_geography import current_areas


def short_name(value):
    parts = value.replace("서울특별시", "").replace("서울시", "").strip().split()
    return parts[-1] if parts else "서울"


def dong_family(value):
    return re.sub(r"(?:[0-9]+가)?[0-9]*동$", "", value)


async def resolve_destination(readers, query):
    async with readers() as db:
        rows = (
            await db.execute(
                select(
                    AdministrativeArea.dong_code,
                    AdministrativeArea.name,
                    AdministrativeArea.sigungu_code,
                ).where(*current_areas())
            )
        ).all()
        if not rows:
            raise ServiceError("DATA_UNAVAILABLE", "서울 지역 경계 자료를 확인하지 못했어요.")
        districts = (
            await db.execute(
                select(RegionBoundary.region_code, RegionBoundary.name).where(
                    RegionBoundary.code_system == "sgis",
                    RegionBoundary.region_code.startswith("11"),
                    func.length(RegionBoundary.region_code) == 5,
                    RegionBoundary.is_active.is_(True),
                    RegionBoundary.processed_at.is_not(None),
                )
            )
        ).all()
    raw = query.strip()
    normalized = re.sub(r"\s*(?:근처|주변|일대)\s*$", "", raw).strip()
    if raw in {"서울", "서울시", "서울특별시", "서울 전체", "전체"}:
        return DestinationArea(label="서울", area_ids=sorted(r[0] for r in rows))
    q = short_name(normalized)
    # Legal-dong word order (성수동1가) can identify the same verified subdivisions.
    q = re.sub(r"^(.+)동([0-9]+)가$", r"\1\2가", q)
    district_names = {short_name(n): code for code, n in districts}
    district = next((code for n, code in district_names.items() if n in raw.split()), None)
    available = [r for r in rows if district is None or r[2] == district]
    if q in district_names:
        matches = [r for r in rows if r[2] == district_names[q]]
    else:
        matches = [r for r in available if short_name(r[1]) == q]
        if not matches and re.search(r"[0-9]+가$", q):
            matches = [
                r for r in available if re.fullmatch(re.escape(q) + r"[0-9]+동", short_name(r[1]))
            ]
        if not matches and len(dong_family(q)) >= 2:
            # '성수/성수동' selects its actual administrative subdivisions, not a guessed point.
            matches = [r for r in available if dong_family(short_name(r[1])) == dong_family(q)]
        if len({r[2] for r in matches}) > 1:
            return None
    if not matches:
        return None
    return DestinationArea(label=raw, area_ids=sorted(r[0] for r in matches))
