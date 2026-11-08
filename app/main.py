from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import router
from app.core.config import Settings
from app.db.session import create_engine, read_session_factory, write_session_factory
from app.modules.recommendation.progress import MemoryProgressStore
from app.services.driving_requests import DrivingRequests


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings if settings is not None else Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(config)
        app.state.session_factory = read_session_factory(engine)
        app.state.write_session_factory = write_session_factory(engine)
        app.state.recommendation_progress = MemoryProgressStore()
        app.state.driving_requests = DrivingRequests(
            budget=config.mobility_request_budget,
            concurrency=config.mobility_max_concurrency,
            interval=1 / config.mobility_requests_per_second,
        )
        try:
            yield
        finally:
            await engine.dispose()

    app = FastAPI(
        title=config.app_name,
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if config.docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if config.docs_enabled else None,
    )

    app.state.settings = config
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Result-Context"],
    )
    app.include_router(router)
    return app
