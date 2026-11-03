from datetime import UTC, datetime

from app.modules.data_pipeline.normalizers.parking import (
    MUNICIPAL_PARKING_SOURCE,
    attach_municipal_positions,
    normalize_parking,
)


def test_municipal_coordinates_require_a_unique_position_and_preserve_raw_rows():
    rows = [{"PKLT_CD": code} for code in ("unique", "ambiguous", "missing")]
    enriched = attach_municipal_positions(
        rows,
        {
            "unique": {(37.56, 126.97)},
            "ambiguous": {(37.56, 126.97), (37.57, 126.98)},
        },
    )
    assert enriched[0] == {"PKLT_CD": "unique", "LAT": 37.56, "LOT": 126.97}
    assert enriched[1:] == rows[1:]
    assert all("LAT" not in row and "LOT" not in row for row in rows)


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
    assert record["details"]["CUR_PRK_CNT"] == 125
    assert record["details"]["CUR_PRK_YN"] == "Y"
    row["NOW_PRK_VHCL_CNT"] = 201
    invalid = normalize_parking(row, now, MUNICIPAL_PARKING_SOURCE)
    assert invalid["details"]["CUR_PRK_CNT"] is None
