from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import router
from app.core.config import Settings
from app.db.session import create_engine, read_session_factory, write_session_factory
from app.modules.ai.providers.factory import open_ai_provider
from app.modules.recommendation.progress import MemoryProgressStore
from app.services.driving_requests import DrivingRequests
from app.services.origin_context import OriginContextStore
from app.services.postgres_origin_context import PostgresOriginContextStore
from app.services.postgres_progress import PostgresProgressStore
from app.services.query_cursors import PostgresQueryCursorStore
from app.services.read_replay import PostgresReadReplay, ReadReplay


def install_state_stores(app: FastAPI, config: Settings) -> None:
    """In-flight API state: trip progress, origin drafts, cursors and P03 replays."""
    if config.state_backend == "memory":
        app.state.recommendation_progress = MemoryProgressStore()
        app.state.origin_contexts = OriginContextStore()
        app.state.query_cursors = None  # each service keeps its own in-process cursors
        app.state.travel_replay = ReadReplay(ttl=config.result_context_ttl_seconds)
        return
    # Shared through PostgreSQL, so a trip continues on any API process and across a
    # rolling restart.
    writers = app.state.write_session_factory
    app.state.recommendation_progress = PostgresProgressStore(writers)
    app.state.origin_contexts = PostgresOriginContextStore(writers)
    app.state.query_cursors = PostgresQueryCursorStore(writers)
    app.state.travel_replay = PostgresReadReplay(writers, ttl=config.result_context_ttl_seconds)


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings if settings is not None else Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(config)
        app.state.session_factory = read_session_factory(engine)
        app.state.write_session_factory = write_session_factory(engine)
        install_state_stores(app, config)
        app.state.driving_requests = DrivingRequests(
            budget=config.mobility_request_budget,
            concurrency=config.mobility_max_concurrency,
            interval=1 / config.mobility_requests_per_second,
        )
        try:
            async with open_ai_provider(config) as provider:
                app.state.ai_provider = provider
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
        expose_headers=["Age", "Retry-After"],
    )
    app.include_router(router)
    return app
