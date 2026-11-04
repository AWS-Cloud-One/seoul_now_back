"""Typed population observations and latest attempts, independent of response files/logs."""

from datetime import UTC, datetime

from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import insert

from app.models import PopulationArea, PopulationAttemptLatest, PopulationLatest
from app.modules.data_pipeline.identity import PROCESSOR_VERSION


class PopulationWriter:
    def __init__(self, session):
        self.session = session

    def has_area(self, area):
        return bool(
            self.session.scalar(
                select(PopulationArea.area_code).where(
                    PopulationArea.area_code == area, PopulationArea.is_active.is_(True)
                )
            )
        )

    def observation(self, area, values, received_at):
        table = PopulationLatest.__table__
        stmt = insert(table).values(
            source="seoul_citydata",
            area_code=area,
            received_at=received_at,
            processed_at=datetime.now(UTC),
            processor_version=PROCESSOR_VERSION,
            **values,
        )
        observed = values["observed_at"]
        where = and_(
            or_(table.c.received_at.is_(None), table.c.received_at <= received_at),
            or_(table.c.observed_at.is_(None), table.c.observed_at <= observed)
            if observed is not None
            else table.c.observed_at.is_(None),
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["source", "area_code"],
            set_={
                key: stmt.excluded[key]
                for key in (*values, "received_at", "processed_at", "processor_version")
            },
            where=where,
        )
        return self.session.scalar(stmt.returning(table.c.area_code)) is not None

    def attempt(self, area, attempted_at, status, error_code, updated=False):
        if not self.has_area(area):
            return
        table = PopulationAttemptLatest.__table__
        stmt = insert(table).values(
            area_code=area,
            attempted_at=attempted_at,
            status=status,
            error_code=error_code,
            observation_updated=updated,
            processor_version=PROCESSOR_VERSION,
        )
        self.session.execute(
            stmt.on_conflict_do_update(
                index_elements=["area_code"],
                set_={
                    key: stmt.excluded[key]
                    for key in (
                        "attempted_at",
                        "status",
                        "error_code",
                        "observation_updated",
                        "processor_version",
                    )
                },
                where=table.c.attempted_at <= attempted_at,
            )
        )
