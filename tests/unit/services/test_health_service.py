import asyncio
import unittest
from unittest.mock import AsyncMock, Mock

from app.core.exceptions import DatabaseUnavailableError
from app.services.health import HealthService


class HealthServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_available_database_is_ready(self) -> None:
        repository = Mock(check_connection=AsyncMock())
        service = HealthService(repository, timeout_seconds=1)

        await service.check_readiness()

        repository.check_connection.assert_awaited_once_with()

    async def test_unavailable_database_stays_unavailable(self) -> None:
        repository = Mock(check_connection=AsyncMock(side_effect=DatabaseUnavailableError))
        service = HealthService(repository, timeout_seconds=1)

        with self.assertRaises(DatabaseUnavailableError):
            await service.check_readiness()

    async def test_readiness_deadline_cancels_slow_database_check(self) -> None:
        cancelled = asyncio.Event()

        async def wait_for_database() -> None:
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        repository = Mock(check_connection=AsyncMock(side_effect=wait_for_database))
        service = HealthService(repository, timeout_seconds=0.01)

        with self.assertRaises(DatabaseUnavailableError):
            await service.check_readiness()

        self.assertTrue(cancelled.is_set())
