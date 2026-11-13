"""Verified destination scopes. Legacy label/area_ids payloads remain readable."""

from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from app.schemas.common import ContractModel
from app.schemas.location import Coordinates


class RegionAnchor(ContractModel):
    place_id: str
    name: str
    coordinates: Coordinates
    address: str
    category: str
    collected_at: AwareDatetime


class DestinationArea(ContractModel):
    label: str = Field(min_length=1, max_length=160)
    kind: Literal["administrative", "official", "radius"] = "administrative"
    query: str | None = None
    area_ids: list[str] = Field(default_factory=list, max_length=500)
    boundary_ids: list[str] = Field(default_factory=list, max_length=3)
    boundary_versions: dict[str, str] = Field(default_factory=dict)
    source: str | None = None
    alias_version: str | None = None
    anchor: RegionAnchor | None = None
    radius_m: int | None = Field(default=None, strict=True, ge=100, le=5000)

    @model_validator(mode="after")
    def coherent_scope(self):
        if self.kind == "administrative":
            valid = bool(self.area_ids) and not self.boundary_ids and self.anchor is None
        elif self.kind == "official":
            valid = (
                not self.area_ids
                and bool(self.boundary_ids)
                and self.anchor is None
                and set(self.boundary_ids) == set(self.boundary_versions)
                and self.source == "citydata_place_boundaries"
            )
        else:
            valid = (
                not self.area_ids
                and not self.boundary_ids
                and self.anchor is not None
                and self.radius_m is not None
                and self.source == "kakao_local"
            )
        if not valid or (self.kind != "radius" and self.radius_m is not None):
            raise ValueError("Destination identity must match its spatial source")
        return self


class DestinationOption(ContractModel):
    position: int = Field(strict=True, ge=1, le=3)
    region: DestinationArea
    description: str


class RegionResolution(ContractModel):
    status: Literal[
        "resolved", "ambiguous", "not_found", "unavailable", "out_of_scope", "needs_input"
    ]
    region: DestinationArea | None = None
    options: list[DestinationOption] = Field(default_factory=list, max_length=3)
    message: str = ""


class RegionSearchScope(ContractModel):
    """V2 spatial snapshot; a radius is never a V1 origin fallback or admin area."""

    region: DestinationArea
    more_available: bool
