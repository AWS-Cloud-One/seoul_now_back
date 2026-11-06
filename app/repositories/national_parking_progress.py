"""Current national parking cursors and page fingerprints; no response/report file reads."""

import re
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert

from app.models import CollectionCursor, CollectionCursorPage
from app.modules.data_pipeline.normalizers.national_parking import METHODS, SOURCE

STATIC_METHODS = METHODS[:2]


async def load_national_parking_progress(session):
    # One SELECT keeps headers and fingerprints on the same committed snapshot.
    rows = await session.execute(
        select(CollectionCursor, CollectionCursorPage)
        .outerjoin(CollectionCursorPage)
        .where(CollectionCursor.source == SOURCE)
        .order_by(CollectionCursor.target_key, CollectionCursorPage.page)
        .execution_options(populate_existing=True)
    )
    result = {}
    for cursor, page in rows:
        if cursor.target_key not in STATIC_METHODS:
            continue
        if cursor.target_key not in result:
            result[cursor.target_key] = dict(
                scan_id=str(cursor.scan_id),
                next_page=cursor.next_page,
                page_size=cursor.page_size,
                provider_total=cursor.provider_total,
                consistent_total=cursor.consistent_total,
                page_hashes={},
                pending_end=dict(
                    page=cursor.pending_page,
                    digest=cursor.pending_digest,
                    total=cursor.pending_total,
                )
                if cursor.pending_page is not None
                else None,
            )
        if page is not None:
            result[cursor.target_key]["page_hashes"][str(page.page)] = page.digest
    return result


def validate_cursor(state):
    def integer(value, minimum=0):
        return type(value) is int and value >= minimum

    def digest(value):
        return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{64}", value))

    try:
        scan = UUID(state["scan_id"])
        valid = (
            integer(state["next_page"], 1)
            and integer(state["page_size"], 1)
            and state["page_size"] <= 1000
            and type(state["consistent_total"]) is bool
            and (state["provider_total"] is None or integer(state["provider_total"]))
            and isinstance(state["page_hashes"], dict)
        )
        if not valid:
            raise ValueError
        hashes = state["page_hashes"]
        pages = {int(page) for page in hashes}
        if (
            len(pages) != len(hashes)
            or any(
                str(int(page)) != page or int(page) < 1 or not digest(value)
                for page, value in hashes.items()
            )
            or pages != set(range(1, state["next_page"]))
        ):
            raise ValueError
        pending = state["pending_end"]
        if pending is not None and (
            not isinstance(pending, dict)
            or pending.get("page") != state["next_page"]
            or not digest(pending.get("digest"))
            or not integer(pending.get("total"))
        ):
            raise ValueError
    except AttributeError, KeyError, TypeError, ValueError:
        raise ValueError("invalid_national_parking_progress") from None
    return scan


async def save_national_parking_progress(session, states, previous):
    """Caller owns the pagination lock and the transaction containing the data writes."""
    for method, state in states.items():
        if method not in STATIC_METHODS:
            raise ValueError("invalid_national_parking_method")
        scan = validate_cursor(state)
        before = previous.get(method)
        if state == before:
            continue
        page_filter = (
            CollectionCursorPage.source == SOURCE,
            CollectionCursorPage.target_key == method,
        )
        changed_scan = before is None or before["scan_id"] != str(scan)
        if changed_scan:
            # The FK includes scan_id: remove the old scan's pages before changing it.
            await session.execute(delete(CollectionCursorPage).where(*page_filter))
        else:
            removed = set(before["page_hashes"]) - set(state["page_hashes"])
            if removed:
                await session.execute(
                    delete(CollectionCursorPage).where(
                        *page_filter,
                        CollectionCursorPage.page.in_([int(page) for page in removed]),
                    )
                )
        pending = state["pending_end"] or {}
        values = dict(
            source=SOURCE,
            target_key=method,
            scan_id=scan,
            next_page=state["next_page"],
            page_size=state["page_size"],
            provider_total=state["provider_total"],
            consistent_total=state["consistent_total"],
            pending_page=pending.get("page"),
            pending_digest=pending.get("digest"),
            pending_total=pending.get("total"),
            updated_at=datetime.now(UTC),
        )
        stmt = insert(CollectionCursor).values(**values)
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=["source", "target_key"],
                set_={
                    key: getattr(stmt.excluded, key)
                    for key in values
                    if key not in {"source", "target_key"}
                },
            )
        )
        old_hashes = {} if changed_scan else before["page_hashes"]
        pages = [
            dict(source=SOURCE, target_key=method, scan_id=scan, page=int(page), digest=digest)
            for page, digest in state["page_hashes"].items()
            if old_hashes.get(page) != digest
        ]
        for start in range(0, len(pages), 1000):
            stmt = insert(CollectionCursorPage).values(pages[start : start + 1000])
            await session.execute(
                stmt.on_conflict_do_update(
                    index_elements=["source", "target_key", "page"],
                    set_={"digest": stmt.excluded.digest, "scan_id": stmt.excluded.scan_id},
                )
            )
