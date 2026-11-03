"""Active normalized SQLAlchemy registry, revision 0009."""

from .areas import AdministrativeArea, PopulationArea, PopulationLatest, RegionBoundary
from .base import Base
from .chat import (
    ChatConditionAgeGroup,
    ChatConditionPurpose,
    ChatConditionVersion,
    ChatExcludedPlace,
    ChatMessage,
    ChatSession,
)
from .collection import (
    CollectionRejectionReason,
    CollectionRun,
    DataSource,
    LegacyArchive,
    RawPayload,
)
from .map import MapFeature, MapLayerVersion, MapPopulationObservation, MapRoadObservation
from .parking import ParkingFacility, ParkingSourceRecord
from .places import Place, PlaceCategory
from .traffic import RoadAreaMapping, RoadLink, RoadLinkMapping, RoadTrafficLatest

__all__ = [
    "ChatConditionAgeGroup",
    "ChatConditionPurpose",
    "ChatConditionVersion",
    "ChatExcludedPlace",
    "ChatMessage",
    "ChatSession",
    "Place",
    "PlaceCategory",
    "AdministrativeArea",
    "CollectionRun",
    "CollectionRejectionReason",
    "LegacyArchive",
    "DataSource",
    "MapFeature",
    "MapLayerVersion",
    "MapPopulationObservation",
    "MapRoadObservation",
    "ParkingFacility",
    "ParkingSourceRecord",
    "PopulationArea",
    "PopulationLatest",
    "RawPayload",
    "RegionBoundary",
    "RoadAreaMapping",
    "RoadLink",
    "RoadLinkMapping",
    "RoadTrafficLatest",
    "Base",
]
