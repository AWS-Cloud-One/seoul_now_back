"""Store the newest full OA-21285 response for each official hotspot."""

from datetime import datetime

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.citydata import CityDataSnapshot


class CityDataRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def upsert(
        self, area_code: str, area_name: str, data: dict, collected_at: datetime
    ) -> None:
        statement = insert(CityDataSnapshot).values(
            area_code=area_code,
            area_name=area_name,
            data=data,
            collected_at=collected_at,
        )
        statement = statement.on_conflict_do_update(
            index_elements=[CityDataSnapshot.area_code],
            set_={
                "area_name": statement.excluded.area_name,
                "data": statement.excluded.data,
                "collected_at": statement.excluded.collected_at,
            },
            where=CityDataSnapshot.collected_at < statement.excluded.collected_at,
        )
        await self.session.execute(statement)

    async def list_latest(self) -> list[CityDataSnapshot]:
        from sqlalchemy import select

        result = await self.session.scalars(
            select(CityDataSnapshot).order_by(CityDataSnapshot.area_name)
        )
        return list(result)

    async def get(self, area_code: str) -> CityDataSnapshot | None:
        from sqlalchemy import select

        return await self.session.scalar(
            select(CityDataSnapshot).where(CityDataSnapshot.area_code == area_code)
        )
