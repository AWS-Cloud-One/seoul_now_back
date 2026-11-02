"""L01/L02 provider conversion. Cursor policy remains an unconfigured port."""

from dataclasses import dataclass
from typing import Protocol

from pydantic import ValidationError

from app.api.contract import ContractError
from app.clients.kakao import KakaoError, KakaoLocalClient
from app.schemas.location import Coordinates, Location
from app.schemas.locations import LocationResolveData, LocationSearchData


def provider_error():
    return ContractError(502, "PROVIDER_ERROR", "출발지 검색 자료를 확인할 수 없습니다.")


def provider_failure(exc: KakaoError) -> ContractError:
    code = {429: "RATE_LIMITED", 504: "PROVIDER_TIMEOUT", 503: "SERVICE_UNAVAILABLE"}.get(
        exc.status,
        "PROVIDER_ERROR",
    )
    return ContractError(exc.status, code, exc.message, retry_after_seconds=exc.retry_after_seconds)


def documents(body):
    docs, meta = body.get("documents"), body.get("meta")
    if (
        not isinstance(docs, list)
        or not all(isinstance(d, dict) for d in docs)
        or not isinstance(meta, dict)
        or type(meta.get("is_end")) is not bool
    ):
        raise provider_error()
    if not docs and not meta["is_end"]:
        raise provider_error()
    return docs, meta["is_end"]


def text(value):
    return value.strip() or None if isinstance(value, str) else None


@dataclass(frozen=True)
class SearchPage:
    documents: list[dict]
    has_more: bool
    next_cursor: str | None = None


class LocationPagination(Protocol):
    async def search(
        self,
        provider: KakaoLocalClient,
        query: str,
        cursor: str | None,
        limit: int,
    ) -> SearchPage:
        """Team-owned cursor encoding/storage/expiry; preserve query and limit."""
        ...


async def first_page(provider: KakaoLocalClient, query: str, limit: int) -> SearchPage:
    # Keep the existing keyword search as the first provider. When no places
    # match, try the address endpoint. Never silently truncate a provider page.
    rows, is_end = documents(await provider.keyword(query, page=1, size=min(limit + 1, 15)))
    if rows:
        if not is_end and len(rows) <= limit and limit > 15:
            more, is_end = documents(await provider.keyword(query, page=2, size=15))
            rows += more
        return SearchPage(rows, not is_end or len(rows) > limit)
    rows, is_end = documents(await provider.address(query, size=limit + 1))
    return SearchPage(rows, not is_end or len(rows) > limit)


def region_from_coordinates(body):
    docs = body.get("documents")
    if not isinstance(docs, list) or not all(isinstance(d, dict) for d in docs):
        raise provider_error()
    # Require coordinate-derived region data, never parse a display address or
    # guess from a broad rectangle. H/B responses must agree on the province.
    names = {text(d.get("region_1depth_name")) for d in docs if d.get("region_type") in {"B", "H"}}
    if len(names) != 1 or None in names:
        raise provider_error()
    name = names.pop()
    supported = {"서울특별시": "seoul", "인천광역시": "incheon", "경기도": "gyeonggi"}
    if name in supported:
        return supported[name]
    if name in {
        "부산광역시",
        "대구광역시",
        "광주광역시",
        "대전광역시",
        "울산광역시",
        "세종특별자치시",
        "강원도",
        "강원특별자치도",
        "충청북도",
        "충청남도",
        "전라북도",
        "전북특별자치도",
        "전라남도",
        "경상북도",
        "경상남도",
        "제주특별자치도",
    }:
        return "other"
    raise provider_error()


class LocationSearchService:
    def __init__(self, provider: KakaoLocalClient, pagination: LocationPagination | None = None):
        self.provider, self.pagination = provider, pagination

    async def search(self, query: str, cursor: str | None, limit: int) -> LocationSearchData:
        if cursor is not None and self.pagination is None:
            raise self.pagination_unavailable()
        try:
            page = (
                await self.pagination.search(self.provider, query, cursor, limit)
                if self.pagination
                else (await first_page(self.provider, query, limit))
            )
            if page.has_more and not page.next_cursor:
                raise self.pagination_unavailable()
            if (not page.has_more and page.next_cursor is not None) or len(page.documents) > limit:
                raise provider_error()
            items, regions = [], {}
            for row in page.documents:
                coords = Coordinates(latitude=row["y"], longitude=row["x"])
                label = text(row.get("place_name")) or text(row.get("address_name"))
                if label is None:
                    raise provider_error()
                key = (coords.latitude, coords.longitude)
                if key not in regions:
                    regions[key] = region_from_coordinates(await self.provider.region(*key))
                region = regions[key]
                items.append(
                    Location(
                        label=label,
                        coordinates=coords,
                        address=text(row.get("road_address_name")) or text(row.get("address_name")),
                        region=region,
                        supported=region in {"seoul", "incheon", "gyeonggi"},
                    )
                )
            return LocationSearchData(items=items, next_cursor=page.next_cursor)
        except KakaoError as exc:
            raise provider_failure(exc) from None
        except ValidationError, KeyError, TypeError:
            raise provider_error() from None

    @staticmethod
    def pagination_unavailable():
        return ContractError(
            503,
            "SERVICE_UNAVAILABLE",
            "다음 페이지 처리가 아직 연결되지 않았습니다. 검색어를 더 구체적으로 입력해 주세요.",
            retryable=False,
        )


def address_from_coordinates(body):
    docs = body.get("documents")
    if not isinstance(docs, list) or not all(isinstance(d, dict) for d in docs):
        raise provider_error()
    # A coordinate response describes one point. Do not arbitrarily select
    # conflicting addresses or manufacture a label when no address is provided.
    addresses = set()
    for row in docs:
        for key in ("road_address", "address"):
            item = row.get(key)
            if item is not None and not isinstance(item, dict):
                raise provider_error()
        road = row.get("road_address") or {}
        land = row.get("address") or {}
        address = text(road.get("address_name")) or text(land.get("address_name"))
        if address is not None:
            addresses.add(address)
    if len(addresses) != 1:
        raise provider_error()
    return addresses.pop()


class LocationResolveService:
    def __init__(self, provider: KakaoLocalClient):
        self.provider = provider

    async def resolve(self, coordinates: Coordinates) -> LocationResolveData:
        try:
            # The original point is retained; administrative-area centers from
            # the region response must never replace the user's coordinates.
            region = region_from_coordinates(
                await self.provider.region(
                    coordinates.latitude,
                    coordinates.longitude,
                )
            )
            address = address_from_coordinates(
                await self.provider.coordinate_address(
                    coordinates.latitude,
                    coordinates.longitude,
                )
            )
            return LocationResolveData(
                location=Location(
                    label=address,
                    address=address,
                    coordinates=coordinates,
                    region=region,
                    supported=region in {"seoul", "incheon", "gyeonggi"},
                )
            )
        except KakaoError as exc:
            raise provider_failure(exc) from None
        except ValidationError, KeyError, TypeError:
            raise provider_error() from None
