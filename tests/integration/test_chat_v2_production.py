"""Main-app V2 wiring, shared budget and role boundary on disposable PostGIS."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy import create_engine, select

from app.core.config import Settings
from app.main import create_app
from app.models.chat_v2 import ChatUsageBucket
from app.modules.chat_v2.budgets import BudgetedInterpreter, ChatBudgets
from app.modules.chat_v2.control import RunControl, active_control
from app.modules.chat_v2.interpretation import InterpretationFailure
from app.modules.chat_v2.runtime import ChatRuntime
from app.modules.chat_v2.storage_contracts import StorageRejected
from infra.scripts.w4_database import TABLES, apply_table_grants
from tests.integration.test_chat_v2_dialogue import Harness
from tests.integration.test_chat_v2_lifecycle import request

pytestmark = pytest.mark.integration


def test_main_app_routes_lifecycle_and_admission(normalized_database, monkeypatch):
    async def run():
        h = Harness(normalized_database)
        rt = ChatRuntime(h.store, h.execution)
        config = Settings(_env_file=None, database_url=normalized_database, chat_v2_enabled=True)
        budgets = ChatBudgets(h.store.writers, config, h.store.clock)
        h.store.budgets = budgets

        @asynccontextmanager
        async def provider(*args, **kwargs):
            yield None

        @asynccontextmanager
        async def runtime(*args, **kwargs):
            yield rt

        monkeypatch.setattr("app.main.open_ai_provider", provider)
        monkeypatch.setattr("app.modules.chat_v2.api.open_runtime", runtime)
        app = create_app(config)
        try:
            async with app.router.lifespan_context(app):
                assert rt.loop_task is not None
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as http:
                    origin = {"Origin": "http://localhost:5173"}
                    preflight = await http.options(
                        "/api/v2/chat/conversations",
                        headers={
                            **origin,
                            "Access-Control-Request-Method": "POST",
                            "Access-Control-Request-Headers": "authorization,content-type",
                        },
                    )
                    assert preflight.status_code == 200
                    issued = await http.post("/api/v2/chat/sessions", headers=origin)
                    assert issued.status_code == 201
                    assert issued.headers["cache-control"] == "no-store"
                    token = issued.json()["session_token"]
                    conv = await http.post(
                        "/api/v2/chat/conversations", headers={"Authorization": f"Bearer {token}"}
                    )
                    assert conv.status_code == 201
                    url = "/api/v2/chat/conversations/" + conv.json()["conversation_id"]
                    assert (await http.get(url)).status_code == 401
                    other = (await http.post("/api/v2/chat/sessions")).json()["session_token"]
                    assert (
                        await http.get(url, headers={"Authorization": f"Bearer {other}"})
                    ).status_code == 404
                    for path in (
                        "/api/v1/chat/messages",
                        "/api/v1/recommendations",
                        "/api/v1/recommendations/retry-routes",
                    ):
                        rejected = await http.post(
                            path, headers=origin, json={"trip_id": conv.json()["trip_id"]}
                        )
                        assert rejected.status_code == 410
                        assert rejected.headers["access-control-allow-origin"] == origin["Origin"]
                    for _ in range(3):
                        assert (await http.post("/api/v2/chat/sessions")).status_code == 201
                    limited = await http.post("/api/v2/chat/sessions")
                    assert limited.status_code == 429
                    assert limited.headers["retry-after"] == "60"
                    assert (await http.get("/api/v1/health/live")).status_code == 200
            assert rt.loop_task is None and not rt.jobs
        finally:
            await h.close()

    asyncio.run(run())


def test_budget_atomic_across_workers_and_does_not_call_model(normalized_database):
    async def run():
        h = Harness(normalized_database)
        config = Settings(_env_file=None, chat_model_calls_per_day=2)
        budgets = ChatBudgets(h.store.writers, config, h.store.clock)
        try:
            results = await asyncio.gather(
                *(budgets.model(32_000) for _ in range(3)), return_exceptions=True
            )
            assert sum(value is None for value in results) == 1
            assert sum(isinstance(value, StorageRejected) for value in results) == 2
            model = SimpleNamespace(input_limit_bytes=32_000, interpret=AsyncMock())
            with pytest.raises(InterpretationFailure, match="INTERPRETATION_FAILED"):
                await BudgetedInterpreter(model, budgets).interpret(None, None)
            model.interpret.assert_not_awaited()
            async with h.store.writers() as db:
                count = await db.scalar(
                    select(ChatUsageBucket.used).where(
                        ChatUsageBucket.bucket == "model_calls:global"
                    )
                )
                assert count == 2
        finally:
            await h.close()

    asyncio.run(run())


def test_chat_tables_are_private_to_api(normalized_database):
    command.check(Config("alembic.ini"))
    engine = create_engine(normalized_database)
    tables = [name for name in TABLES if name.startswith("chat_")]
    try:
        with engine.connect() as connection:
            connection.begin()
            conn = connection.connection.driver_connection
            try:
                for role in ("api", "import", "collector"):
                    name = "chat_grants_" + uuid4().hex
                    user = sql.Identifier(name)
                    conn.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(user))
                    conn.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(user))
                    for table in tables:
                        apply_table_grants(conn, role, table, name)
                    conn.execute(sql.SQL("SET LOCAL ROLE {}").format(user))
                    for table in tables:
                        statement = sql.SQL("SELECT * FROM {} LIMIT 0").format(
                            sql.Identifier(table)
                        )
                        if role == "api":
                            conn.execute(statement)
                            conn.execute(
                                sql.SQL("DELETE FROM {} WHERE false").format(sql.Identifier(table))
                            )
                        else:
                            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                                with conn.transaction():
                                    conn.execute(statement)
                    if role == "api":
                        conn.execute(
                            "INSERT INTO chat_usage_buckets "
                            "VALUES ('probe', now(), 1, now()+interval '2 days')"
                        )
                        conn.execute("UPDATE chat_usage_buckets SET used=2 WHERE bucket='probe'")
                    conn.execute("RESET ROLE")
            finally:
                connection.rollback()
    finally:
        engine.dispose()


def test_run_replay_is_free_and_http_budget_stops_transport(normalized_database):
    async def run():
        h = Harness(normalized_database)
        # Keep all admissions in one fixed window, even across a wall-clock minute.
        now = h.store.clock()
        h.store.clock = lambda: now
        config = Settings(_env_file=None, chat_http_calls_per_run=1)
        budgets = ChatBudgets(h.store.writers, config, h.store.clock)
        h.store.budgets = budgets
        try:
            await h.start()
            for _ in range(6):
                body = await request(h)
                view = await h.store.admit(
                    h.credential, h.conversation.conversation_id, body, dialogue=True, queued=True
                )
                await h.store.cancel(
                    h.credential, h.conversation.conversation_id, view.identity.run_id
                )
            replay = await h.store.admit(
                h.credential, h.conversation.conversation_id, body, dialogue=True, queued=True
            )
            assert replay.identity.run_id == view.identity.run_id
            with pytest.raises(StorageRejected, match="RATE_LIMITED"):
                await h.store.admit(
                    h.credential,
                    h.conversation.conversation_id,
                    await request(h),
                    dialogue=True,
                    queued=True,
                )
            transport = AsyncMock(return_value=httpx.Response(200))
            control = RunControl(
                h.store, SimpleNamespace(context=SimpleNamespace(identity=view.identity))
            )
            token = active_control.set(control)
            try:
                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(transport),
                    event_hooks={"request": [budgets.http_request]},
                ) as http:
                    assert (await http.get("https://example.test/place")).status_code == 200
                    with pytest.raises(httpx.RequestError, match="budget exhausted"):
                        await http.get("https://example.test/place")
                assert transport.await_count == 1
            finally:
                active_control.reset(token)
        finally:
            await h.close()

    asyncio.run(run())
