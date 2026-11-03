import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from jobs.publish_map import build_parking


class Rows(list):
    def all(self):
        return self


def run(status="partial"):
    return SimpleNamespace(
        source="seoul_getparkinfo",
        area_code=None,
        status=status,
        started_at=datetime.now(UTC),
        rejected=725 if status == "partial" else 0,
    )


def test_partial_collection_publishes_valid_records():
    session = MagicMock()
    session.scalars = AsyncMock(side_effect=[Rows([run()]), []])
    session.flush = AsyncMock()
    record = SimpleNamespace(
        id="original-fingerprint",
        source="seoul_getparkinfo",
        name="등록 주차장",
        address="서울",
        details={"TPKCT": "10"},
        source_updated_at=None,
        collected_at=datetime.now(UTC),
    )
    session.execute = AsyncMock(
        return_value=[
            (
                record,
                '{"type":"Point","coordinates":[126.978,37.5665]}',
            )
        ]
    )
    info = {}
    features, _ = asyncio.run(build_parking(session, info))
    assert len(features) == 1
    assert features[0]["properties"]["name"] == record.name
    assert info == {"partial_runs": 1, "rejected_rows": 725}
    source_record = session.add.call_args_list[-1].args[0]
    assert source_record.legacy_record_id == record.id
    assert source_record.registered["collected_at"] == record.collected_at.isoformat()


def test_partial_omission_keeps_previously_published_record():
    session = MagicMock()
    mapping = SimpleNamespace(
        legacy_record_id="previous",
        source="seoul_getparkinfo",
        parking_id=uuid4(),
        is_active=True,
    )
    previous = SimpleNamespace(is_active=True, properties={"name": "기존 주차장"})
    shape = {"type": "Point", "coordinates": [126.978, 37.5665]}
    session.scalars = AsyncMock(side_effect=[Rows([run()]), [mapping]])
    session.execute = AsyncMock(return_value=[])
    session.get = AsyncMock(return_value=previous)
    session.scalar = AsyncMock(return_value=json.dumps(shape))
    features, _ = asyncio.run(build_parking(session))
    assert mapping.is_active is True
    assert features[0]["id"] == str(mapping.parking_id)
    assert features[0]["geometry"] == shape


def test_no_completed_valid_collection_does_not_deactivate_records():
    session = MagicMock()
    session.scalars = AsyncMock(return_value=Rows())
    with pytest.raises(RuntimeError, match="No completed parking collection"):
        asyncio.run(build_parking(session))
    session.add.assert_not_called()


def test_full_snapshot_still_deactivates_missing_record():
    session = MagicMock()
    mapping = SimpleNamespace(
        legacy_record_id="removed",
        source="seoul_getparkinfo",
        parking_id=uuid4(),
        is_active=True,
    )
    facility = SimpleNamespace(identity_status="unverified", is_active=True)
    session.scalars = AsyncMock(side_effect=[Rows([run("success")]), [mapping]])
    session.execute = AsyncMock(return_value=[])
    session.get = AsyncMock(return_value=facility)
    features, _ = asyncio.run(build_parking(session))
    assert features == []
    assert mapping.is_active is False
    assert facility.is_active is False
