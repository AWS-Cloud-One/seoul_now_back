"""Complete reference replacement keeps observations but retires stale current relations."""

import asyncio
from copy import deepcopy
from datetime import date

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import (
    AdministrativeArea,
    MapFeature,
    MapLayerVersion,
    RoadAreaMapping,
    RoadLink,
    RoadLinkMapping,
    RoadTrafficLatest,
)
from app.modules.data_pipeline.pipelines.snapshots import save_snapshot
from app.repositories.reference import ReferenceRepository
from app.services.map_publication import MapPublicationService
from tests.fixtures.road_reference import road_candidate

pytestmark = pytest.mark.integration
LINE = {"type": "LineString", "coordinates": [[127, 37.5], [127.01, 37.5]]}


def candidate(links, version="2026", mapping="2026-11-01"):
    return road_candidate(
        {
            "features": [{"id": link, "geometry": LINE} for link in links],
            "mapping_rows": [
                {"service_link_id": link, "sequence": 0, "standard_link_id": standard}
                for link, standard in links.items()
            ],
        },
        version=version,
        mapping_version=mapping,
    )


async def store(factory, data):
    async with factory.begin() as session:
        return await save_snapshot(
            session, "topis_geometry:" + data["reference"]["inputs"]["vertices"]["version"], data
        )


def test_replacement_retires_old_road_mapping_and_intersections_but_keeps_observation(
    normalized_database,
):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        try:
            async with factory.begin() as s:
                await save_snapshot(
                    s,
                    "sgis_areas",
                    {
                        "features": [
                            {
                                "id": "dong",
                                "properties": {"name": "동", "sigungu_code": "district"},
                                "geometry": {
                                    "type": "Polygon",
                                    "coordinates": [
                                        [
                                            [126.9, 37.4],
                                            [127.2, 37.4],
                                            [127.2, 37.6],
                                            [126.9, 37.6],
                                            [126.9, 37.4],
                                        ]
                                    ],
                                },
                            }
                        ]
                    },
                )
            first = await store(factory, candidate({"a": "x", "b": "y"}, "2025", "2025-03-06"))
            assert first.status == "success"
            async with factory.begin() as s:
                traffic = await save_snapshot(
                    s, "topis:b", {"rows": [{"link_id": "b", "prcs_spd": "0"}]}
                )
                assert await MapPublicationService(s).publish("roads") == 2
            new = candidate({"a": "z"})
            new["reference"]["inputs"]["vertices"]["reference_date"] = "2026-10-01"
            new["reference"]["inputs"]["mapping"]["reference_date"] = "2026-11-01"
            result = await store(factory, new)
            assert result.status == "success"
            async with factory.begin() as s:
                assert await ReferenceRepository(s).topis_links() == ["a"]
                assert await ReferenceRepository(s).has_prerequisite("topis_geometry")
                a, b = await s.get(RoadLink, "a"), await s.get(RoadLink, "b")
                assert a.source_version == "2026" and a.reference_date == date(2026, 10, 1)
                assert a.last_payload_id == result.payload_id and not b.is_active
                assert (
                    await s.get(RoadTrafficLatest, ("topis_traffic", "b"))
                ).payload_id == traffic.payload_id
                mappings = list(await s.scalars(select(RoadLinkMapping)))
                assert len(mappings) == 1 and mappings[0].standard_link_id == "z"
                assert mappings[0].source_version == "2026-11-01"
                assert mappings[0].reference_date == date(2026, 11, 1)
                areas = list(await s.scalars(select(RoadAreaMapping)))
                assert {r.link_id for r in areas} == {"a"}
                area_version = areas[0].road_geometry_version
                assert (
                    await s.get(AdministrativeArea, "dong")
                ).road_mapping_version == area_version
                # Publication remains a separate B13 step, but uses the same current set.
                assert await s.scalar(select(func.count()).select_from(MapFeature)) == 2
                # Even a legacy leftover active flag cannot leak into publication/intersections.
                b.is_active = True
                await s.flush()
                assert await MapPublicationService(s).publish("roads") == 1
                assert set(await s.scalars(select(RoadAreaMapping.link_id))) == {"a"}
                assert (await s.get(MapLayerVersion, "roads")).geometry_version == area_version
            repeat = await store(factory, new)
            assert repeat.status == "success"
            async with factory() as s:
                assert await ReferenceRepository(s).topis_links() == ["a"]
                assert await s.scalar(select(func.count()).select_from(RoadLink)) == 2
                assert await s.scalar(select(func.count()).select_from(RoadLinkMapping)) == 1
                assert await s.scalar(select(RoadAreaMapping.road_geometry_version)) == area_version
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.parametrize(
    "failure",
    [
        "partial",
        "empty",
        "excluded",
        "unmatched",
        "geometry",
        "missing_provenance",
        "missing_spatial",
    ],
)
def test_refused_candidate_keeps_complete_current_reference(normalized_database, failure):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        try:
            previous = await store(factory, candidate({"a": "x", "b": "y"}))
            new = candidate({"a": "z"})
            if failure == "partial":
                new["reference"]["complete"] = False
            elif failure == "empty":
                new = candidate({})
            elif failure == "excluded":
                new["report"].update(file_links=2, excluded_sequence_gap_links=["b"])
            elif failure == "unmatched":
                new["mapping_rows"].append(
                    {"service_link_id": "other", "sequence": 0, "standard_link_id": "q"}
                )
            elif failure == "geometry":
                new["features"][0]["geometry"] = {"type": "Point", "coordinates": [127, 37.5]}
            elif failure == "missing_spatial":
                new["report"].pop("spatial_validation")
            else:
                new["reference"]["inputs"].pop("mapping")
            refused = await store(factory, new)
            assert refused.status == "failed" and not refused.processing["updated"]
            async with factory() as s:
                assert await ReferenceRepository(s).topis_links() == ["a", "b"]
                assert (await s.get(RoadLink, "a")).last_payload_id == previous.payload_id
                assert set(await s.scalars(select(RoadLinkMapping.standard_link_id))) == {"x", "y"}
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_reviewed_exclusion_and_ambiguous_mapping_keep_separate_quality_counts(normalized_database):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        try:
            await store(factory, candidate({"a": "x", "b": "y"}))
            new = candidate({"a": "z"})
            new["mapping_rows"] += [
                {"service_link_id": "a", "sequence": 0, "standard_link_id": "q"},
                {"service_link_id": "b", "sequence": 0, "standard_link_id": "y"},
            ]
            new["report"].update(
                file_links=2,
                excluded_sequence_gap_links=["b"],
                ambiguous_mapping_positions=[{"service_link_id": "a", "sequence": 0}],
            )
            held = await store(factory, new)
            assert held.status == "failed"
            approved = deepcopy(new)
            approved["reference"]["allowed_exclusions"] = ["b"]
            applied = await store(factory, approved)
            assert applied.status == "partial" and applied.processing["updated"]
            assert (
                applied.processing["received"],
                applied.processing["accepted"],
                applied.processing["rejected"],
            ) == (2, 1, 1)
            async with factory() as s:
                assert await ReferenceRepository(s).topis_links() == ["a"]
                assert set(await s.scalars(select(RoadLinkMapping.standard_link_id))) == {"z", "q"}
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_older_pending_import_cannot_replace_a_newer_committed_reference(
    normalized_database, monkeypatch
):
    from app.repositories.collection import CollectionRepository

    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        waiting, resume = asyncio.Event(), asyncio.Event()
        original_archive = CollectionRepository.archive

        async def pause_old(self, run_id, data, *args, **kwargs):
            result = await original_archive(self, run_id, data, *args, **kwargs)
            if (
                data.get("reference", {}).get("inputs", {}).get("vertices", {}).get("version")
                == "old"
            ):
                waiting.set()
                await resume.wait()
            return result

        try:
            await store(factory, candidate({"a": "x"}))
            monkeypatch.setattr(CollectionRepository, "archive", pause_old)
            old = asyncio.create_task(store(factory, candidate({"old": "o"}, "old")))
            try:
                await asyncio.wait_for(waiting.wait(), 5)
                latest = await store(factory, candidate({"new": "n"}, "new"))
            finally:
                resume.set()
            refused = await asyncio.wait_for(old, 5)
            assert latest.status == "success"
            assert refused.error_code == "road_reference_time_guard"
            async with factory() as s:
                assert await ReferenceRepository(s).topis_links() == ["new"]
                assert set(await s.scalars(select(RoadLinkMapping.standard_link_id))) == {"n"}
        finally:
            await engine.dispose()

    asyncio.run(run())
