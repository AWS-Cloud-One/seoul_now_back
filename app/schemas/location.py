"""Location contract shared by origin search and structured recommendation input."""

from typing import Literal

from pydantic import Field, field_validator

from app.schemas.common import ContractModel


class Coordinates(ContractModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)

    @field_validator("latitude", "longitude", mode="before")
    @classmethod
    def reject_booleans(cls, value):
        if isinstance(value, bool):
            raise ValueError("Coordinates must be numbers, not booleans")
        return value


class Location(ContractModel):
    label: str
    coordinates: Coordinates
    address: str | None
    region: Literal["seoul", "incheon", "gyeonggi", "other"] | None
    supported: bool = Field(strict=True)
