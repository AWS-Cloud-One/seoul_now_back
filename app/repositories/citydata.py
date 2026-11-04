"""Read selected CITYDATA sections with original evidence and latest-attempt metadata."""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import aliased

from app.models import CollectionRun, RawPayload
from app.modules.data_pipeline.normalizers.citydata import CityDataError
from app.modules.data_pipeline.normalizers.citydata_sections import (
    CONTRACT,
    select_citydata,
    unwrap_citydata,
)
from app.storage.payloads import configured_store


@dataclass
class CityDataSnapshot:
    area_code: str
    area_name: str
    data: dict
    collected_at: datetime
    payload_id: object = None
    received_at: datetime | None = None
    selected_payload_id: object = None
    run_id: object = None
    evidence: dict = field(default_factory=dict)


class CityDataRepository:
    def __init__(self, session):
        self.session = session
        self.store = configured_store()

    async def list_latest(self, area_code=None, *, section=None, current_run_ids=None):
        condition = (
            CollectionRun.target_key == "area:" + area_code
            if area_code
            else CollectionRun.target_key.like("area:%")
        )
        attempts = list(
            await self.session.scalars(
                select(CollectionRun)
                .where(CollectionRun.source == "seoul_citydata", condition)
                .distinct(CollectionRun.target_key)
                .order_by(
                    CollectionRun.target_key,
                    CollectionRun.started_at.desc(),
                    CollectionRun.id.desc(),
                )
            )
        )
        latest = {r.target_key.removeprefix("area:"): r for r in attempts}
        selected = aliased(RawPayload)
        stamp = func.coalesce(
            RawPayload.received_at, RawPayload.legacy_collected_at, RawPayload.stored_at
        )
        rows = await self.session.stream(
            select(RawPayload, selected, CollectionRun, stamp)
            .join(CollectionRun, RawPayload.run_id == CollectionRun.id)
            .outerjoin(
                selected,
                (selected.parent_payload_id == RawPayload.payload_id)
                & (selected.part_key == "selected")
                & (selected.payload_kind == "derived_snapshot"),
            )
            .where(
                CollectionRun.source == "seoul_citydata",
                condition,
                CollectionRun.status != "running",
                RawPayload.format == "json",
                RawPayload.document_type == "object",
                RawPayload.payload_kind.in_(["provider_response", "legacy_snapshot"]),
            )
            .order_by(
                CollectionRun.target_key,
                stamp.desc(),
                RawPayload.stored_at.desc(),
                RawPayload.payload_id.desc(),
            )
        )
        chosen, attempt_states = {}, {}
        async for original, derived, run, stamp in rows:
            code = run.target_key.removeprefix("area:")
            if code in chosen and run.id != latest[code].id:
                continue
            if section is None and run.status not in {"success", "partial"}:
                continue
            if derived:
                selection = await asyncio.to_thread(self.store.load_json, derived)
                if selection.get("contract") != CONTRACT or selection.get(
                    "input_payload_id"
                ) != str(original.payload_id):
                    raise ValueError("invalid_citydata_selected_evidence")
            else:
                if run.status not in {"success", "partial"}:
                    continue
                body = await asyncio.to_thread(self.store.load_json, original)
                try:
                    selection = select_citydata(
                        unwrap_citydata(body, expected_code=code), original.received_at
                    )
                except CityDataError:
                    continue
            city = selection["CITYDATA"]
            state = selection["sections"][section]["state"] if section else "usable"
            if run.id == latest[code].id:
                attempt_states[code] = state
            if code in chosen:
                continue
            if section and state not in {"usable", "empty"}:
                continue
            chosen[code] = CityDataSnapshot(
                code,
                city["AREA_NM"],
                city,
                stamp,
                original.payload_id,
                original.received_at,
                derived.payload_id if derived else None,
                run.id,
            )
        result = []
        for code, attempt in latest.items():
            snapshot = chosen.get(code)
            if snapshot is None:
                if section is None:
                    continue
                snapshot = CityDataSnapshot(code, "", {}, attempt.started_at)
            state = attempt_states.get(
                code, "failed" if attempt.status == "failed" else "unavailable"
            )
            snapshot.evidence = {
                "area_code": code,
                "section": section,
                "input_payload_id": str(snapshot.payload_id) if snapshot.payload_id else None,
                "selected_payload_id": str(snapshot.selected_payload_id)
                if snapshot.selected_payload_id
                else None,
                "input_run_id": str(snapshot.run_id) if snapshot.run_id else None,
                "received_at": snapshot.received_at.isoformat() if snapshot.received_at else None,
                "latest_attempt": {
                    "run_id": str(attempt.id),
                    "status": attempt.status,
                    "state": state,
                    "error_code": attempt.error_code,
                },
                "reused_previous": snapshot.run_id is not None and snapshot.run_id != attempt.id,
                "current_cycle": str(snapshot.run_id) == (current_run_ids or {}).get(code)
                if current_run_ids is not None
                else None,
                "selected_contract": CONTRACT,
            }
            result.append(snapshot)
        return result

    async def get(self, area_code):
        return next(iter(await self.list_latest(area_code)), None)
