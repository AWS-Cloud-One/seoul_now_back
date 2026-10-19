import asyncio
from typing import TYPE_CHECKING

from app.core.exceptions import DatabaseUnavailableError

if TYPE_CHECKING:
    from app.repositories.health import HealthRepository


class HealthService:
    def __init__(self, repository: HealthRepository, timeout_seconds: float) -> None:
        self._repository = repository
        self._timeout_seconds = timeout_seconds

    async def check_readiness(self) -> None:
        """Require the database check to complete within the readiness time limit."""
        try:
            async with asyncio.timeout(self._timeout_seconds):
                await self._repository.check_connection()
        except TimeoutError:
            raise DatabaseUnavailableError from None
