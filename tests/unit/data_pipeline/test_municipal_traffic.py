from datetime import UTC, datetime

from app.modules.data_pipeline.normalizers.parking import (
    MUNICIPAL_PARKING_SOURCE,
    normalize_parking,
)
from app.modules.data_pipeline.normalizers.traffic import road_feature
from app.repositories.parking import parking_availability


def test_municipal_occupancy_is_subtracted_not_exposed_as_vacancy():
    now = datetime.now(UTC)
    row = {
        "PKLT_CD": "a",
        "PKLT_NM": "test",
        "LAT": 37.56,
        "LOT": 126.97,
        "TPKCT": 200.0,
        "NOW_PRK_VHCL_CNT": 75.0,
        "NOW_PRK_VHCL_UPDT_TM": now.isoformat(),
        "PRK_STTS_YN": "1",
    }
    record = normalize_parking(row, now, MUNICIPAL_PARKING_SOURCE)
    availability = parking_availability(
        MUNICIPAL_PARKING_SOURCE, record["details"], record["source_updated_at"], now
    )
    assert availability["available_spaces"] == 125
    row["NOW_PRK_VHCL_CNT"] = 201
    invalid = normalize_parking(row, now, MUNICIPAL_PARKING_SOURCE)
    assert (
        parking_availability(
            MUNICIPAL_PARKING_SOURCE, invalid["details"], invalid["source_updated_at"], now
        )["available_spaces"]
        is None
    )


def test_road_geometry_preserves_longitude_latitude_and_rejects_invalid():
    row = {"LINK_ID": "1234567890", "XYLIST": "126.97_37.56|126.98_37.57", "SPD": 24, "IDX": "서행"}
    feature = road_feature(row, "POI009", "2026-10-22T00:00:00Z", None)
    assert feature["geometry"]["coordinates"][0] == [126.97, 37.56]
    row["XYLIST"] = "NaN_37.56|126.98_37.57"
    assert road_feature(row, "POI009", None, None) is None
