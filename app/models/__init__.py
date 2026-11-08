"""Normalized models matching revision 0013, including recommendation snapshots."""

from .areas import (
    AdministrativeArea,
    PopulationArea,
    PopulationAttemptLatest,
    PopulationLatest,
    RegionBoundary,
)
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
)
from .collection_progress import CollectionCursor, CollectionCursorPage
from .map import MapFeature, MapLayerVersion, MapPopulationObservation, MapRoadObservation
from .parking import (
    ParkingAvailabilityLatest,
    ParkingFacility,
    ParkingFieldIssue,
    ParkingOperatingHour,
    ParkingSourceRecord,
)
from .places import Place, PlaceCategory
from .recommendation import RecommendationCard, RecommendationEvidence, RecommendationResult
from .sensors import PedestrianSensor, PedestrianSensorLatest
from .traffic import (
    RoadAreaMapping,
    RoadLink,
    RoadLinkMapping,
    RoadTrafficAttemptLatest,
    RoadTrafficLatest,
)

__all__ = [
    "RecommendationResult",
    "RecommendationCard",
    "RecommendationEvidence",
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
    "CollectionCursor",
    "CollectionCursorPage",
    "CollectionRejectionReason",
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
    "PopulationAttemptLatest",
    "RegionBoundary",
    "RoadAreaMapping",
    "RoadLink",
    "RoadLinkMapping",
    "RoadTrafficLatest",
    "RoadTrafficAttemptLatest",
    "Base",
]
