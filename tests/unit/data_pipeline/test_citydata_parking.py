from datetime import UTC, datetime

from app.modules.data_pipeline.normalizers.parking import (
    CITYDATA_PARKING_SOURCE,
    normalize_parking,
)


def test_normalizes_current_parking_fields_and_marks_source_time():
    row = {
        "PRK_NM": "샘플 주차장",
        "PRK_CD": "3202173",
        "CPCTY": "24",
        "CUR_PRK_CNT": "7",
        "CUR_PRK_TIME": "2026-10-22 12:00:00",
        "CUR_PRK_YN": "Y",
        "PAY_YN": "Y",
        "RATES": "2000",
        "TIME_RATES": "30",
        "ADD_RATES": "1000",
        "ADD_TIME_RATES": "10",
        "ADDRESS": "중구 북창동 18-9",
        "ROAD_ADDR": "",
        "LNG": "126.977556",
        "LAT": "37.562981",
    }
    normalized = normalize_parking(row, datetime.now(UTC), CITYDATA_PARKING_SOURCE)
    assert normalized["source_id"] == "3202173"
    assert normalized["details"]["CUR_PRK_CNT"] == "7"
    assert normalized["details"]["CUR_PRK_YN"] == "Y"
    assert normalized["source_updated_at"].isoformat().startswith("2026-10-22T12:00:00")
