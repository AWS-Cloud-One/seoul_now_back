"""Opt-in spatial integration checks. Every test rolls back its DB writes."""

import asyncio
import json
import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.contract import ContractError
from app.core.config import Settings
from app.db.session import create_engine
from app.models.map import MapFeature, MapLayerVersion, MapObservation
from app.repositories.map import MapRepository
from app.schemas.map import MapQuery
from app.services.map_contract import MapService

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_MAP_DB_TESTS") != "1",
        reason="Set RUN_MAP_DB_TESTS=1 for rollback-only PostGIS tests",
    ),
]


@pytest.mark.parametrize("layer", ["roads", "population"])
def test_spatial_selection_and_version_consistency(layer):
    async def check():
        engine = create_engine(Settings())
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
                                layer=layer, geometry_version="", coverage="", is_ready=False
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
                        for feature_id, active in [(key, True), (key + "-inactive", False)]:
                            own_props = props | {
                                "link_id" if layer == "roads" else "area_code": feature_id
                            }
                            session.add(
                                MapFeature(
                                    layer=layer,
                                    feature_id=feature_id,
                                    is_active=active,
                                    properties=own_props,
                                    geometry=func.ST_SetSRID(
                                        func.ST_GeomFromGeoJSON(json.dumps(shape)), 4326
                                    ),
                                )
                            )
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
                            MapObservation(
                                layer=layer,
                                feature_id=key,
                                value=value,
                                observed_at=datetime.now(UTC),
                                collected_at=datetime.now(UTC),
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
                        assert (
                            geometry.feature_collection.features[0].geometry.model_dump(mode="json")
                            == shape
                        )
                        if layer == "population":
                            hole = await service.geometry(
                                layer, MapQuery(bbox=(1.5, 1.5, 2.5, 2.5), level=6)
                            )
                            assert hole.feature_collection.features == []
                        with pytest.raises(ContractError) as error:
                            await service.status(layer, query, "old-version")
                        assert error.value.code == "GEOMETRY_VERSION_MISMATCH"
                finally:
                    if transaction.is_active:
                        await transaction.rollback()
        finally:
            await engine.dispose()

    asyncio.run(check())
