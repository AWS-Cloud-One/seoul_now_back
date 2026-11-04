"""Read committed pagination state from the existing immutable processing reports."""

import asyncio

from sqlalchemy import select

from app.models import CollectionRun, RawPayload
from app.storage.payloads import configured_store

STATIC_METHODS = ("PrkSttusInfo", "PrkOprInfo")


async def load_national_parking_progress(session):
    payload = await session.scalar(
        select(RawPayload)
        .join(CollectionRun, CollectionRun.id == RawPayload.run_id)
        .where(
            CollectionRun.source == "national_parking",
            CollectionRun.target_key == "all",
            CollectionRun.finished_at.is_not(None),
            RawPayload.payload_kind == "processing_report",
            RawPayload.part_key == "report",
        )
        .order_by(
            CollectionRun.started_at.desc(),
            RawPayload.stored_at.desc(),
            RawPayload.payload_id.desc(),
        )
        .limit(1)
    )
    if payload is None:
        return {}
    document = await asyncio.to_thread(configured_store().load_json, payload)
    if not isinstance(document, dict):
        raise ValueError("invalid_national_parking_progress")
    report = document.get("pagination_progress")
    if report is None:
        # Reports written before resumable pagination have no checkpoint.
        return {}
    if not isinstance(report, dict) or report.get("version") != 1:
        raise ValueError("invalid_national_parking_progress")
    state = report.get("state")
    if not isinstance(state, dict) or set(state) - set(STATIC_METHODS):
        raise ValueError("invalid_national_parking_progress")
    for cursor in state.values():
        if (
            not isinstance(cursor, dict)
            or not isinstance(cursor.get("scan_id"), str)
            or type(cursor.get("next_page")) is not int
            or cursor["next_page"] < 1
            or type(cursor.get("page_size")) is not int
            or not 1 <= cursor["page_size"] <= 1000
            or not isinstance(cursor.get("page_hashes"), dict)
            or type(cursor.get("consistent_total")) is not bool
        ):
            raise ValueError("invalid_national_parking_progress")
    return state
