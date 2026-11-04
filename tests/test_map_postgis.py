"""Opt-in spatial integration checks. Every test rolls back its DB writes."""

import asyncio
import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import ServiceError
from app.db.session import create_engine
from app.models import (
    MapFeature,
    MapLayerVersion,
    MapPopulationObservation,
    MapRoadObservation,
    PopulationArea,
    RoadLink,
)
from app.repositories.collection import CollectionRepository
from app.repositories.map import MapRepository
from app.schemas.map import MapQuery
from app.services.map import MapService

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("layer", ["roads", "population"])
def test_spatial_selection_and_version_consistency(layer, normalized_database):
    async def check():
        engine = create_engine(Settings(_env_file=None, database_url=normalized_database))
        try:
            async with engine.connect() as connection:
                transaction = await connection.begin()
                try:
                    async with AsyncSession(
                        bind=connection, join_transaction_mode="rollback_only"
                    ) as session:
                        version = await session.get(MapLayerVersion, layer)
                        if version is None:
                            version = MapLayerVersion(
                                layer=layer, geometry_version=None, coverage="", is_ready=False
                            )
                            session.add(version)
                        version.geometry_version = "rollback-test"
                        version.coverage = "Temporary test; rolled back"
                        version.is_ready = True
                        await session.flush()
                        key = "rollback-" + uuid4().hex
                        shape = (
                            {"type": "LineString", "coordinates": [[-1, 0.5], [2, 0.5]]}
                            if layer == "roads"
                            else {
                                "type": "Polygon",
                                "coordinates": [
                                    [[0, 0], [4, 0], [4, 4], [0, 4], [0, 0]],
                                    [[1, 1], [3, 1], [3, 3], [1, 3], [1, 1]],
                                ],
                            }
                        )
                        props = (
                            {"link_id": key, "name": None, "direction": None}
                            if layer == "roads"
                            else {
                                "area_code": key,
                                "name": "Test area",
                            }
                        )
                        repo = CollectionRepository(session)
                        run = await repo.start("spatial_fixture", "all")
                        payload = await repo.archive(run.id, {}, datetime.now(UTC))
                        for feature_id, active in [(key, True), (key + "-inactive", False)]:
                            geom = func.ST_Multi(
                                func.ST_SetSRID(func.ST_GeomFromGeoJSON(json.dumps(shape)), 4326)
                            )
                            parent = (
                                RoadLink(link_id=feature_id)
                                if layer == "roads"
                                else PopulationArea(area_code=feature_id, name="Test area")
                            )
                            parent.geometry = geom
                            parent.source = "spatial_fixture"
                            parent.is_active = active
                            session.add(parent)
                            await session.flush()
                            session.add(
                                MapFeature(
                                    layer=layer,
                                    feature_id=feature_id,
                                    is_active=active,
                                    name=props["name"],
                                    geometry=geom,
                                    **{
                                        "road_link_id"
                                        if layer == "roads"
                                        else "population_area_code": feature_id
                                    },
                                )
                            )
                        await session.flush()
                        value = (
                            {"speed_kmh": 0, "travel_seconds": 25}
                            if layer == "roads"
                            else {
                                "min_people": 0,
                                "max_people": 10,
                                "congestion": "normal",
                                "is_replacement": None,
                            }
                        )
                        session.add(
                            (MapRoadObservation if layer == "roads" else MapPopulationObservation)(
                                layer=layer,
                                **{"link_id" if layer == "roads" else "area_code": key},
                                **value,
                                has_observation=True,
                                source="spatial_fixture",
                                payload_id=payload.payload_id,
                                observed_at=datetime.now(UTC),
                                processed_at=datetime.now(UTC),
                                processor_version="test",
                                collection_failed=False,
                            )
                        )
                        await session.flush()
                        service = MapService(MapRepository(session))
                        query = MapQuery(bbox=(0.1, 0.1, 0.9, 0.9), level=6)
                        geometry = await service.geometry(layer, query)
                        status = await service.status(layer, query, "rollback-test")
                        assert [f.id for f in geometry.feature_collection.features] == [key]
                        ids = [i.link_id if layer == "roads" else i.area_code for i in status.items]
                        assert ids == [key]
                        assert status.status == "ok"
                        assert geometry.feature_collection.features[0].geometry.model_dump(
                            mode="json"
                        ) == {
                            "type": "MultiLineString" if layer == "roads" else "MultiPolygon",
                            "coordinates": [shape["coordinates"]],
                        }
                        if layer == "population":
                            hole = await service.geometry(
                                layer, MapQuery(bbox=(1.5, 1.5, 2.5, 2.5), level=6)
                            )
                            assert hole.feature_collection.features == []
                        with pytest.raises(ServiceError) as error:
                            await service.status(layer, query, "old-version")
                        assert error.value.code == "GEOMETRY_VERSION_MISMATCH"
                finally:
                    if transaction.is_active:
                        await transaction.rollback()
        finally:
            await engine.dispose()

    asyncio.run(check())
