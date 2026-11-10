"""Storage bounds on every source count, including unusable live-attempt values."""

from datetime import UTC, datetime

import pytest

from app.modules.data_pipeline.normalizers import national_parking
from app.modules.data_pipeline.normalizers.parking import (
    CITYDATA_PARKING_SOURCE,
    MUNICIPAL_PARKING_SOURCE,
    SOURCE,
)
from app.modules.data_pipeline.normalizers.parking_attributes import (
    ParkingCountOutOfRange,
    normalize_attributes,
    parking_count,
)
from app.modules.data_pipeline.normalizers.values import number

STAMP = datetime(2026, 11, 10, tzinfo=UTC)
COUNT_FIELDS = [
    (SOURCE, "TPKCT"),
    (MUNICIPAL_PARKING_SOURCE, "TPKCT"),
    (MUNICIPAL_PARKING_SOURCE, "NOW_PRK_VHCL_CNT"),
    (CITYDATA_PARKING_SOURCE, "CPCTY"),
    (CITYDATA_PARKING_SOURCE, "CUR_PRK_CNT"),
    ("national_facility", "prk_cmprt_co"),
    ("national_live", "pkfc_ParkingLots_total"),
    ("national_live", "pkfc_Available_ParkingLots_total"),
]


@pytest.mark.parametrize("source,key", COUNT_FIELDS)
@pytest.mark.parametrize(
    "value",
    [2**31, str(2**31), "2147483648.0", "1e100000", pytest.param(2**20000, id="huge-int")],
)
def test_overflow_rejects_source_row_even_when_live_data_is_unusable(source, key, value):
    row = {key: value}
    with pytest.raises(ParkingCountOutOfRange, match="^parking_count_out_of_range$"):
        if source == "national_facility":
            national_parking.facility(
                {
                    "prk_center_id": "test",
                    "prk_plce_nm": "test",
                    "prk_plce_adres_sido": "서울특별시",
                    "prk_plce_entrc_la": "37.56",
                    "prk_plce_entrc_lo": "126.97",
                    **row,
                }
            )
        elif source == "national_live":
            national_parking.availability(row, STAMP)
        else:
            # No provision flag or timestamp: these facts still reach attempt columns.
            normalize_attributes(row, STAMP, source)


@pytest.mark.parametrize(
    "value,expected,reason",
    [
        (0, 0, None),
        ("0", 0, None),
        (2**31 - 1, 2**31 - 1, None),
        ("2147483647.0", 2**31 - 1, None),
        (None, None, "MISSING_VALUE"),
        ("", None, "MISSING_VALUE"),
        (True, None, "INVALID_NUMBER"),
        (-1, None, "INVALID_NUMBER"),
        ("1.5", None, "INVALID_INTEGER"),
        ("NaN", None, "INVALID_NUMBER"),
        ("Infinity", None, "INVALID_NUMBER"),
    ],
)
def test_boundary_zero_and_existing_missing_invalid_semantics(value, expected, reason):
    parsed = parking_count(value)
    assert (parsed.value, parsed.reason) == (expected, reason)


def test_count_bound_does_not_change_unbounded_or_decimal_field_parsing():
    assert number(2**31, integer=True).value == 2**31
    assert number("2147483648.5").value > 2**31
    assert number("1e100000", integer=True, maximum=2**31 - 1).reason == "INTEGER_OUT_OF_RANGE"
