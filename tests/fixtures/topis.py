"""Synthetic typed road setup and current observations; no archived response fixtures."""

from datetime import UTC, datetime

from sqlalchemy import text

from app.modules.data_pipeline.normalizers.topis import traffic_observation
from app.modules.data_pipeline.pipelines.topis_storage import finish_cycle, store_batch, summary
from app.repositories.collection import CollectionRepository


async def seed_roads(factory, links):
    async with factory.begin() as session:
        await session.execute(
            text("""
            INSERT INTO data_sources(source_code,display_name,provider_name)
            VALUES ('topis_geometry','test','test') ON CONFLICT DO NOTHING
        """)
        )
        for link in links:
            await session.execute(
                text("""
                INSERT INTO road_links
                    (link_id,source,source_version,geometry,processor_version,processed_at)
                VALUES (:link,'topis_geometry','test',
                    ST_GeomFromText('MULTILINESTRING((127 37.5,127.01 37.5))',4326),'test',now())
            """),
                {"link": link},
            )


def response(link, speed=25, travel=30, **extra):
    return {
        "rows": [{"link_id": link, "prcs_spd": str(speed), "prcs_trv_time": str(travel), **extra}]
    }


def entry(link, data, receipt, *, error=None, attempts=1, attempted_at=None):
    result = (
        traffic_observation(link, data, receipt)
        if error is None
        else dict(values=None, status="failed", error_code=error, processing={})
    )
    return dict(
        **result,
        link_id=link,
        attempted_at=attempted_at or receipt or datetime.now(UTC),
        attempts=attempts,
    )


async def write(factory, link, data, receipt, **kwargs):
    async with factory.begin() as session:
        run = await CollectionRepository(session).start("topis_traffic", "network")
        outcomes = await store_batch(session, run.id, [entry(link, data, receipt, **kwargs)])
        await finish_cycle(session, run.id, summary(outcomes, 1))
        return {**outcomes[0], "run_id": run.id}
