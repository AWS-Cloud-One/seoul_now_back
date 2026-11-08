"""Manual-radius facility search; base-fee comparison is not a payable total."""

import hashlib
import json
from datetime import UTC, datetime

from app.core.exceptions import ServiceError
from app.schemas.parking_nearby import ParkingFacility
from app.schemas.query_services import ParkingSearchData, ParkingSearchItem
from app.services.parking_registration import registered_information
from app.services.query_cursors import QUERY_CURSORS


def comparable_fee(records):
    values = set()
    for record in records:
        if record.is_paid is False:
            values.add(0.0)
        elif (
            record.is_paid is True
            and record.base_usable
            and record.base_minutes is not None
            and record.base_minutes > 0
            and record.base_fee_krw is not None
            and record.base_fee_krw > 0
        ):
            values.add(float(record.base_fee_krw * 60 / record.base_minutes))
    if len(values) > 1:
        return None, "conflict", "CONFLICTING_SOURCE_FEES"
    if not values:
        return None, "unknown", "FEE_UNIT_OR_PAYMENT_UNKNOWN"
    value = values.pop()
    return value, "free" if value == 0 else "paid", None


class ParkingSearchService:
    def __init__(self, repository, *, cursors=None, max_facilities=5000):
        self.repository = repository
        self.cursors = cursors if cursors is not None else QUERY_CURSORS
        self.max_facilities = max_facilities

    async def search(self, query):
        scope = ("parking-search", query.model_dump(exclude={"cursor"}))
        cursor = self.cursors.read(query.cursor, scope) if query.cursor else None
        version = await self.repository.get_version()
        if version is None or not version.is_ready:
            raise ServiceError("DATA_UNAVAILABLE", "주차 기준 자료가 준비되지 않았습니다.")
        rows = await self.repository.list_facilities(
            query,
            query.radius_meters,
            limit=self.max_facilities + 1,
            name=(query.name or "").strip() or None,
        )
        if len(rows) > self.max_facilities:
            raise ServiceError(
                "VALIDATION_ERROR", "검색 범위나 이름을 줄여 주세요.", retryable=False
            )
        records = await self.repository.list_records([row[0] for row in rows])
        items = []
        for key, identity, props, lat, lon, meters in rows:
            source_records = records.get(key, [])
            price, status, reason = comparable_fee(source_records)
            facility = ParkingFacility(
                parking_id=key,
                identity_status=identity,
                name=props["name"],
                coordinates={"latitude": lat, "longitude": lon},
                distance_meters=meters,
                records=[
                    {
                        "source_record_id": r.source_record_id,
                        "source": r.source,
                        "reference_date": r.reference_date,
                        "registered": registered_information(r),
                    }
                    for r in source_records
                ],
            )
            items.append(
                ParkingSearchItem(
                    facility=facility,
                    hourly_base_comparison_krw=price,
                    fee_status=status,
                    fee_reason=reason,
                )
            )
        if query.sort == "price":
            items.sort(
                key=lambda item: (
                    item.hourly_base_comparison_krw is None,
                    item.hourly_base_comparison_krw or 0,
                    str(item.facility.parking_id),
                )
            )
        else:
            items.sort(
                key=lambda item: (item.facility.distance_meters, str(item.facility.parking_id))
            )
        digest = hashlib.sha256(
            json.dumps(
                [item.model_dump(mode="json") for item in items], sort_keys=True, ensure_ascii=False
            ).encode()
        ).hexdigest()
        if cursor and (cursor["version"] != version.geometry_version or cursor["digest"] != digest):
            raise ServiceError(
                "CURSOR_EXPIRED", "검색 자료가 변경됐습니다. 첫 페이지부터 조회해 주세요."
            )
        offset = cursor["offset"] if cursor else 0
        end = offset + query.limit
        return ParkingSearchData(
            checked_at=datetime.now(UTC),
            radius_meters=query.radius_meters,
            total_count=len(items),
            items=items[offset:end],
            next_cursor=self.cursors.issue(
                scope, {"version": version.geometry_version, "digest": digest, "offset": end}
            )
            if end < len(items)
            else None,
        )
