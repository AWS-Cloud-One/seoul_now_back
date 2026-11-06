"""Observe actual spatial-column writes, not merely equal results after a rewrite."""

import asyncio
from copy import deepcopy

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import MapFeature, ParkingFacility, ParkingSourceRecord
from app.modules.data_pipeline.normalizers.parking import SOURCE
from app.modules.data_pipeline.pipelines.parking import run_parking

pytestmark = pytest.mark.integration


async def audit(session, tables):
    await session.execute(
        text("CREATE TABLE spatial_write_audit (relation text, identity text, operation text)")
    )
    await session.execute(
        text("""CREATE FUNCTION spatial_write_audit_fn() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
        INSERT INTO spatial_write_audit VALUES
            (TG_TABLE_NAME, to_jsonb(NEW)->>TG_ARGV[0], TG_OP);
        RETURN NEW; END $$""")
    )
    for table, column, identity in tables:
        await session.execute(
            text(
                f"CREATE TRIGGER audit_spatial AFTER UPDATE OF {column} ON {table} "
                f"FOR EACH ROW EXECUTE FUNCTION spatial_write_audit_fn('{identity}')"
            )
        )


async def events(factory):
    async with factory() as session:
        return list(
            (
                await session.execute(
                    text(
                        "SELECT relation,identity FROM spatial_write_audit "
                        "ORDER BY relation,identity"
                    )
                )
            ).all()
        )


def test_parking_same_position_is_never_written_but_real_move_keeps_identity(normalized_database):
    async def run():
        engine = create_async_engine(normalized_database)
        factory = async_sessionmaker(engine)
        rows = [
            dict(
                PKLT_CD=str(i),
                PKLT_NM=f"parking{i}",
                ADDR=f"서울 address{i}",
                LAT=37.56,
                LOT=126.97 + i / 1000,
                TPKCT="100",
            )
            for i in (1, 2)
        ]

        async def fetch():
            return deepcopy(rows)

        try:
            assert (await run_parking(engine, fetch, SOURCE))["accepted"] == 2
            async with factory.begin() as session:
                await audit(
                    session,
                    [
                        ("parking_facilities", "location", "parking_id"),
                        ("parking_source_records", "location", "source_record_id"),
                        ("map_features", "geometry", "feature_id"),
                    ],
                )
                old = {
                    r.provider_record_id: (r.source_record_id, r.parking_id, r.collected_at)
                    for r in await session.scalars(select(ParkingSourceRecord))
                }
                original_features = {
                    r.feature_id: r.published_at for r in await session.scalars(select(MapFeature))
                }
            rows[0]["TPKCT"] = "101"
            result = await run_parking(engine, fetch, SOURCE)
            assert result["accepted"] == 2 and await events(factory) == []
            async with factory() as session:
                record = await session.get(ParkingSourceRecord, old["1"][0])
                assert record.registered_capacity == 101 and record.collected_at != old["1"][2]
                assert {
                    r.feature_id: r.published_at for r in await session.scalars(select(MapFeature))
                } == original_features
            rows[0]["LOT"] += 0.001
            result = await run_parking(engine, fetch, SOURCE)
            assert result["accepted"] == 2
            assert await events(factory) == sorted(
                [
                    ("parking_facilities", str(old["1"][1])),
                    ("parking_source_records", str(old["1"][0])),
                    ("map_features", str(old["1"][1])),
                ]
            )
            async with factory() as session:
                assert {
                    r.provider_record_id: (r.source_record_id, r.parking_id)
                    for r in await session.scalars(select(ParkingSourceRecord))
                } == {k: v[:2] for k, v in old.items()}
                unchanged = await session.get(MapFeature, ("parking", str(old["2"][1])))
                assert unchanged.published_at == original_features[str(old["2"][1])]
                assert await session.get(ParkingFacility, old["1"][1]) is not None
        finally:
            await engine.dispose()

    asyncio.run(run())
