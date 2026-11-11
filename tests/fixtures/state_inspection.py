"""Read in-flight API state in tests, whichever store the app uses.

Every helper returns a snapshot. With the shared (database) stores state lives in
PostgreSQL, so a test must read again after each request instead of holding a
live in-memory reference.
"""

from copy import deepcopy
from uuid import UUID

from sqlalchemy import text

from app.modules.recommendation.progress import MemoryProgressStore
from app.services.origin_context import OriginContextStore


async def trip_state(app, trip_id):
    store = app.state.recommendation_progress
    if isinstance(store, MemoryProgressStore):
        return deepcopy(store.trip(trip_id))
    return await store.peek(UUID(str(trip_id)))


async def trip_request_count(app, trip_id):
    store = app.state.recommendation_progress
    if isinstance(store, MemoryProgressStore):
        return len(store.trip(trip_id).requests)
    return await store.request_count(UUID(str(trip_id)))


async def origin_draft(app, token):
    store = app.state.origin_contexts
    if isinstance(store, OriginContextStore):
        return store.entries[token]
    return await store.peek(token)


async def expire_origin_drafts(app):
    """Move every stored origin draft past its expiry (database clock)."""
    async with app.state.write_session_factory.begin() as session:
        await session.execute(
            text("UPDATE origin_contexts SET expires_at = now() - interval '1 second'")
        )
