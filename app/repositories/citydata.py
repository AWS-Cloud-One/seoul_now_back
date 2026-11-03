"""Read original CITYDATA payloads for parking projection and official-file imports."""

import asyncio
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select

from app.models import CollectionRun, RawPayload
from app.storage.payloads import configured_store


@dataclass
class CityDataSnapshot:
    area_code: str
    area_name: str
    data: dict
    collected_at: datetime
    payload_id: object = None
    received_at: datetime | None = None


class CityDataRepository:
    def __init__(self, session):
        self.session = session
        self.store = configured_store()

    async def list_latest(self, area_code=None):
        stamp = func.coalesce(
            RawPayload.received_at, RawPayload.legacy_collected_at, RawPayload.stored_at
        )
        rows = (
            await self.session.execute(
                select(RawPayload, stamp)
                .join(CollectionRun)
                .where(
                    CollectionRun.source == "seoul_citydata",
                    CollectionRun.target_key == "area:" + area_code
                    if area_code
                    else CollectionRun.target_key.like("area:%"),
                    CollectionRun.status.in_(["success", "partial"]),
                    RawPayload.format == "json",
                    RawPayload.document_type == "object",
                    RawPayload.payload_kind != "legacy_observation",
                )
                .distinct(CollectionRun.target_key)
                .order_by(CollectionRun.target_key, stamp.desc(), RawPayload.stored_at.desc())
            )
        ).all()
        result = []
        for p, t in rows:
            body = await asyncio.to_thread(self.store.load_json, p)
            result.append(
                CityDataSnapshot(
                    str(body["AREA_CD"]), str(body["AREA_NM"]), body, t, p.payload_id, p.received_at
                )
            )
        return result

    async def get(self, area_code):
        return next(
            (s for s in await self.list_latest(area_code) if s.area_code == area_code), None
        )
