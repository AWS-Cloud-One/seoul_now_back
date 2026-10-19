from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.repositories.health import HealthRepository
from app.services.health import HealthService


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    # One session per request. Closing it rolls back unfinished transactions.
    async with request.app.state.session_factory() as session:
        yield session


def get_health_service(
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> HealthService:
    repository = HealthRepository(session)
    return HealthService(repository, timeout_seconds=settings.health_timeout_seconds)
