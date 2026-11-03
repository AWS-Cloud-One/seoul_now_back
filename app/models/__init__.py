"""Register SQLAlchemy models here so Alembic can discover their metadata."""

from app.db.base import Base
from app.models.citydata import CityDataSnapshot
from app.models.map import (
    MapFeature,
    MapLayerVersion,
    MapObservation,
    ParkingFacility,
    ParkingSourceRecord,
)
from app.models.parking import CollectionRun, ParkingRecord
from app.models.population import FloatingPopulationHistory, PopulationArea
from app.models.provider import ProviderSnapshot

__all__ = ["Base", "ProviderSnapshot", "CityDataSnapshot", "CollectionRun", "ParkingRecord"]

from app.models.external_api import ExternalApiObservation, ExternalApiResponse

__all__ += ["ExternalApiResponse", "ExternalApiObservation"]
__all__ += ["PopulationArea", "FloatingPopulationHistory"]
__all__ += [
    "MapFeature",
    "MapLayerVersion",
    "MapObservation",
    "ParkingFacility",
    "ParkingSourceRecord",
]
