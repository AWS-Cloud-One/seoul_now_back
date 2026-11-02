"""L01 draft query and result contract."""

from typing import Annotated

from pydantic import BeforeValidator, Field

from app.schemas.common import ApiEnvelope, ContractModel
from app.schemas.location import Location

SearchQuery = Annotated[str, BeforeValidator(str.strip), Field(min_length=1, max_length=100)]


class LocationSearchData(ContractModel):
    items: list[Location]
    next_cursor: str | None


class LocationSearchResponse(ApiEnvelope[LocationSearchData]):
    data: LocationSearchData
    error: None = None


class LocationResolveData(ContractModel):
    location: Location


class LocationResolveResponse(ApiEnvelope[LocationResolveData]):
    data: LocationResolveData
    error: None = None
