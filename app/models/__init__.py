"""Register SQLAlchemy models here so Alembic can discover their metadata."""

from app.db.base import Base
from app.models.citydata import CityDataSnapshot
from app.models.parking import CollectionRun, ParkingRecord
from app.models.provider import ProviderSnapshot

__all__ = ["Base", "ProviderSnapshot", "CityDataSnapshot", "CollectionRun", "ParkingRecord"]
