"""Recommendation evidence from DB0012 publications, within the shared read snapshot."""

from datetime import UTC, datetime

from geoalchemy2 import Geography
from sqlalchemy import func, select

from app.models import MapFeature, ParkingFacility
from app.repositories.map import MapRepository
from app.repositories.parking_nearby import ParkingNearbyRepository
from app.schemas.location import Coordinates
from app.schemas.parking_nearby import ParkingFacility as FacilityResponse
from app.schemas.recommendation import ParkingEvidence, RoadTrafficEvidence, TrafficEvidence
from app.services.map import information
from app.services.parking_registration import registered_information


class RecommendationEvidenceRepository:
    def __init__(self, session):
        self.session = session
        self.parking = ParkingNearbyRepository(session)
        self.map = MapRepository(session)
        self.parking_ready = self.roads_ready = False

    async def load(self):
        parking = await self.parking.get_version()
        roads = await self.map.get_version("roads")
        self.parking_ready = bool(parking and parking.is_ready and parking.geometry_version)
        self.roads_ready = bool(roads and roads.is_ready and roads.geometry_version)

    async def for_place(self, place):
        coordinates = Coordinates(latitude=place.y, longitude=place.x)
        traffic = await self.traffic(coordinates)
        parking = await self.nearest_parking(coordinates)
        return traffic, parking

    async def nearest_parking(self, coordinates):
        if not self.parking_ready:
            return None
        # Same 300/500/1000m first-available-radius policy as P02, without its
        # incomplete pagination adapter. A top-one query is sufficient for ranking.
        for radius in (300, 500, 1000):
            statement, distance = self.parking.spatial_query(coordinates, radius)
            row = (
                await self.session.execute(
                    statement.with_only_columns(
                        ParkingFacility.parking_id,
                        ParkingFacility.identity_status,
                        MapFeature.name,
                        func.ST_Y(MapFeature.geometry),
                        func.ST_X(MapFeature.geometry),
                        distance,
                    )
                    .order_by(distance, ParkingFacility.parking_id)
                    .limit(1)
                )
            ).first()
            if row is None:
                continue
            key, identity, name, lat, lon, meters = row
            records = (await self.parking.list_records([key]))[key]
            facility = FacilityResponse(
                parking_id=key,
                identity_status=identity,
                name=name,
                coordinates=Coordinates(latitude=lat, longitude=lon),
                distance_meters=meters,
                records=[
                    dict(
                        source_record_id=r.source_record_id,
                        source=r.source,
                        reference_date=r.reference_date,
                        registered=registered_information(r),
                    )
                    for r in records
                ],
            )
            # No provider precedence, invented UUID, or vacancy count.
            return ParkingEvidence(
                id=str(key),
                name=name,
                distance_m=meters,
                facility=facility,
                source=records[0].source if len(records) == 1 else None,
                source_time=records[0].source_updated_at if len(records) == 1 else None,
            )
        return None

    async def traffic(self, coordinates):
        if not self.roads_ready:
            return TrafficEvidence()
        point = func.ST_SetSRID(
            func.ST_MakePoint(coordinates.longitude, coordinates.latitude), 4326
        ).cast(Geography)
        ids = list(
            await self.session.scalars(
                select(MapFeature.feature_id)
                .where(
                    MapFeature.layer == "roads",
                    MapFeature.is_active.is_(True),
                    func.ST_DWithin(MapFeature.geometry.cast(Geography), point, 500),
                )
                .order_by(MapFeature.feature_id)
            )
        )
        observations = await self.map.get_observations("roads", ids)
        now = datetime.now(UTC)
        roads = [
            RoadTrafficEvidence(
                link_id=key, traffic=information("roads", observations.get(key), now)
            )
            for key in ids
        ]
        # Main's source has speed/travel-time, not congestion grades. Never infer
        # 원활/서행/정체 from speed. Reuse its received-at/30-minute freshness policy.
        return TrafficEvidence(road_ids=ids, roads=roads)
