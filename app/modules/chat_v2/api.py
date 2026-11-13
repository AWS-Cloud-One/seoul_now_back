"""V2 HTTP contract: install into the main app or run a standalone local app."""

from contextlib import ExitStack, asynccontextmanager
from datetime import UTC, datetime
from uuid import UUID

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from app.modules.chat_v2.audit import audit_logging
from app.modules.chat_v2.budgets import BudgetedInterpreter, ChatBudgets, peer_address
from app.modules.chat_v2.contracts import RunRequest
from app.modules.chat_v2.http_contracts import CancelRequest, RunPoll, SessionCreated, SubmitResult
from app.modules.chat_v2.storage_contracts import (
    ConversationSnapshot,
    ExecutionView,
    HistoryPage,
    SessionCredential,
    StorageRejected,
)


@asynccontextmanager
async def open_runtime(config=None, *, engine=None, driving_requests=None):
    from app.core.config import Settings
    from app.db.session import create_engine, read_session_factory, write_session_factory
    from app.modules.chat_v2.bedrock import open_bedrock_interpreter
    from app.modules.chat_v2.execution import build_dialogue_execution
    from app.modules.chat_v2.runtime import ChatRuntime
    from app.modules.chat_v2.storage import ChatV2Store, utcnow
    from app.services.driving_requests import DrivingRequests

    config = config if config is not None else Settings()
    owns_engine = engine is None
    engine = engine if engine is not None else create_engine(config)
    budgets = ChatBudgets(write_session_factory(engine), config, utcnow)
    resources = ExitStack()
    resources.enter_context(audit_logging())
    try:
        async with (
            httpx.AsyncClient(event_hooks={"request": [budgets.http_request]}) as http,
            open_bedrock_interpreter(config, mode="dialogue") as interpreter,
        ):
            store = ChatV2Store(write_session_factory(engine), budgets=budgets)
            execution = build_dialogue_execution(
                store,
                BudgetedInterpreter(interpreter, budgets),
                read_session_factory(engine),
                http,
                config,
                driving_requests=driving_requests
                or DrivingRequests(
                    budget=config.mobility_request_budget,
                    concurrency=config.mobility_max_concurrency,
                    interval=1 / config.mobility_requests_per_second,
                ),
            )
            yield ChatRuntime(store, execution)
    finally:
        resources.close()
        if owns_engine:
            await engine.dispose()


def create_app(runtime_factory=open_runtime):
    @asynccontextmanager
    async def lifespan(app):
        async with runtime_factory() as runtime:
            app.state.runtime = runtime
            await runtime.start()
            try:
                yield
            finally:
                await runtime.close()

    app = FastAPI(title="Seoul Now Chat V2 — local integration", lifespan=lifespan)
    install_api(app)
    return app


def install_api(app):
    """Register endpoints and handlers; the caller owns runtime startup/shutdown."""
    prefix = "/api/v2/chat"

    @app.middleware("http")
    async def no_store(request, call_next):
        response = await call_next(request)
        if request.url.path.startswith(prefix):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(StorageRejected)
    async def rejected(request, exc):
        status = {
            "UNAUTHORIZED": 401,
            "NOT_FOUND": 404,
            "EXPIRED": 410,
            "ACTION_EXPIRED": 410,
            "INVALID_CURSOR": 422,
            "COMMIT_UNCERTAIN": 503,
            "PEER_UNAVAILABLE": 503,
            "RATE_LIMITED": 429,
        }.get(exc.code, 409)
        return JSONResponse(
            {"error": {"code": exc.code}},
            status_code=status,
            headers={"Retry-After": "60"} if status == 429 else {},
        )

    @app.exception_handler(SQLAlchemyError)
    async def unavailable(request, exc):
        return JSONResponse(
            {"error": {"code": "STORE_UNAVAILABLE"}}, status_code=503, headers={"Retry-After": "1"}
        )

    def runtime(request: Request):
        return request.app.state.runtime

    def credential(authorization: str | None = Header(default=None)):
        try:
            scheme, value = authorization.split(" ", 1)
            session_id, token = value.split(".", 1)
            if scheme.lower() != "bearer":
                raise ValueError
            return SessionCredential(
                session_id=UUID(session_id), token=token, expires_at=datetime.now(UTC)
            )
        except AttributeError, ValueError:
            raise HTTPException(401, "UNAUTHORIZED") from None

    runtime_dependency = Depends(runtime)
    credential_dependency = Depends(credential)

    @app.post(prefix + "/sessions", status_code=201, response_model=SessionCreated)
    async def session(request: Request, rt=runtime_dependency):
        aws = getattr(getattr(request.app.state, "settings", None), "runtime_env", "local") == "aws"
        cred = await rt.store.create_session(peer=peer_address(request, aws=aws))
        return {
            "session_id": cred.session_id,
            "expires_at": cred.expires_at,
            "session_token": f"{cred.session_id}.{cred.token.get_secret_value()}",
        }

    @app.post(prefix + "/conversations", status_code=201, response_model=ConversationSnapshot)
    async def conversation(cred=credential_dependency, rt=runtime_dependency):
        return await rt.store.create_conversation(cred)

    @app.get(prefix + "/conversations/{conversation_id}", response_model=ConversationSnapshot)
    async def current(conversation_id: UUID, cred=credential_dependency, rt=runtime_dependency):
        return await rt.store.conversation(cred, conversation_id)

    @app.post(
        prefix + "/conversations/{conversation_id}/runs",
        status_code=202,
        response_model=SubmitResult,
    )
    async def submit(
        conversation_id: UUID,
        body: RunRequest,
        request: Request,
        cred=credential_dependency,
        rt=runtime_dependency,
    ):
        aws = getattr(getattr(request.app.state, "settings", None), "runtime_env", "local") == "aws"
        view = await rt.submit(cred, conversation_id, body, peer=peer_address(request, aws=aws))
        return {"run": view, "status_url": f"{prefix}/runs/{view.identity.run_id}"}

    @app.get(prefix + "/conversations/{conversation_id}/messages", response_model=HistoryPage)
    async def messages(
        conversation_id: UUID,
        limit: int = Query(20, ge=1, le=50),
        cursor: str | None = Query(None, max_length=512),
        cred=credential_dependency,
        rt=runtime_dependency,
    ):
        return await rt.store.history(cred, conversation_id, limit=limit, cursor=cursor)

    @app.get(prefix + "/runs/{run_id}", response_model=RunPoll)
    async def run(
        run_id: UUID,
        after_seq: int = Query(0, ge=0, le=200),
        cred=credential_dependency,
        rt=runtime_dependency,
    ):
        conversation_id = await rt.store.locate_run(cred, run_id)
        view, events = await rt.store.events(cred, conversation_id, run_id, after_seq)
        return {"run": view, "events": events}

    @app.post(prefix + "/runs/{run_id}/cancel", response_model=ExecutionView)
    async def cancel(
        run_id: UUID, body: CancelRequest, cred=credential_dependency, rt=runtime_dependency
    ):
        # Idempotent at the run level: different cancellation IDs cannot apply twice.
        conversation_id = await rt.store.locate_run(cred, run_id)
        return await rt.store.cancel(cred, conversation_id, run_id)

    @app.delete(prefix + "/conversations/{conversation_id}", status_code=204)
    async def delete(conversation_id: UUID, cred=credential_dependency, rt=runtime_dependency):
        await rt.store.delete_conversation(cred, conversation_id)
        return Response(status_code=204)

    return app
