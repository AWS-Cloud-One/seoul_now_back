"""Read existing citydata and parking projections without triggering collectors."""

from app.modules.data_pipeline.normalizers.parking import (
    CITYDATA_PARKING_SOURCE,
    MUNICIPAL_PARKING_SOURCE,
)
from app.modules.data_pipeline.normalizers.traffic import snapshot_features
from app.modules.recommendation.evidence import parking_evidence, traffic_evidence
from app.modules.recommendation.ranking import DEFAULT_POLICY
from app.repositories.citydata import CityDataRepository
from app.repositories.parking import ParkingRepository


class RecommendationEvidenceRepository:
    def __init__(self, session, policy=DEFAULT_POLICY):
        self.citydata = CityDataRepository(session)
        self.parking = ParkingRepository(session)
        self.policy = policy
        self.features = []

    async def load(self):
        self.features, _ = snapshot_features(await self.citydata.list_latest())

    async def for_place(self, place):
        traffic = traffic_evidence(place.x, place.y, self.features, policy=self.policy)
        results = []
        if 37.4 <= place.y <= 37.75 and 126.7 <= place.x <= 127.25:
            for source in (CITYDATA_PARKING_SOURCE, MUNICIPAL_PARKING_SOURCE):
                results.append(await self.parking.nearby(place.y, place.x, source, 100))
        return traffic, parking_evidence(results)
