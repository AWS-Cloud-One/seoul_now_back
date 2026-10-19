from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import DatabaseUnavailableError


class HealthRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def check_connection(self) -> None:
        """Verify that both PostgreSQL and PostGIS can serve a query."""
        try:
            await self._session.execute(text("SELECT PostGIS_Version()"))
        except SQLAlchemyError, OSError:
            raise DatabaseUnavailableError from None
