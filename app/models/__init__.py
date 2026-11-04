"""Active normalized registry; schema is installed by Alembic through revision 0011."""

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
    PayloadFileDeletion,
    RawPayload,
)
from .map import MapFeature, MapLayerVersion, MapPopulationObservation, MapRoadObservation
from .parking import (
    ParkingAvailabilityLatest,
    ParkingFacility,
    ParkingFieldIssue,
    ParkingOperatingHour,
    ParkingSourceRecord,
)
from .places import Place, PlaceCategory
from .sensors import PedestrianSensor, PedestrianSensorLatest
from .traffic import (
    RoadAreaMapping,
    RoadLink,
    RoadLinkMapping,
    RoadTrafficAttemptLatest,
    RoadTrafficLatest,
)

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
    "ParkingAvailabilityLatest",
    "ParkingFieldIssue",
    "ParkingOperatingHour",
    "ParkingFacility",
    "ParkingSourceRecord",
    "PedestrianSensor",
    "PedestrianSensorLatest",
    "PopulationArea",
    "PopulationLatest",
    "RawPayload",
    "PayloadFileDeletion",
    "RegionBoundary",
    "RoadAreaMapping",
    "RoadLink",
    "RoadLinkMapping",
    "RoadTrafficLatest",
    "RoadTrafficAttemptLatest",
    "Base",
]
