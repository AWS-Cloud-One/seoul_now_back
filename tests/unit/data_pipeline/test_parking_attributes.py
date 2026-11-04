from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.modules.data_pipeline.normalizers.parking import (
    CITYDATA_PARKING_SOURCE,
    MUNICIPAL_PARKING_SOURCE,
    SOURCE,
    normalize_parking,
)
from app.modules.data_pipeline.normalizers.parking_attributes import (
    normalize_attributes,
    operating_hours,
)
from app.modules.data_pipeline.normalizers.storage_fields import parking_fields

RECEIVED = datetime(2026, 11, 4, 0, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "source,fees",
    [
        (
            SOURCE,
            {
                "PRK_HM": "5",
                "PRK_CRG": "430",
                "ADD_UNIT_TM_MNT": "10",
                "ADD_CRG": "800",
                "DLY_MAX_CRG": "20000",
            },
        ),
        (
            MUNICIPAL_PARKING_SOURCE,
            {
                "BSC_PRK_HR": "5",
                "BSC_PRK_CRG": "430",
                "ADD_PRK_HR": "10",
                "ADD_PRK_CRG": "800",
                "DAY_MAX_CRG": "20000",
            },
        ),
        (
            CITYDATA_PARKING_SOURCE,
            {"TIME_RATES": "5", "RATES": "430", "ADD_TIME_RATES": "10", "ADD_RATES": "800"},
        ),
    ],
)
def test_source_fees_reach_existing_storage_columns_without_changing_identity(source, fees):
    row = {
        "PKLT_CD": "1",
        "PKLT_NM": "주차장",
        "PRK_CD": "1",
        "PRK_NM": "주차장",
        "LAT": 37.56,
        "LOT": 126.97,
        "LNG": 126.97,
        "TPKCT": "50",
        "CPCTY": "50",
        **fees,
    }
    original = deepcopy(row)
    result = normalize_parking(row, RECEIVED, source)
    saved = parking_fields(result)
    assert (
        saved["base_minutes"],
        saved["base_fee_krw"],
        saved["additional_minutes"],
        saved["additional_fee_krw"],
    ) == (5, 430, 10, 800)
    assert saved["daily_max_krw"] == (None if source == CITYDATA_PARKING_SOURCE else 20000)
    assert result["fees"]["base_usable"] and result["fees"]["additional_usable"]
    assert row == original
    assert (
        normalize_parking({**row, **{key: "0" for key in fees}}, RECEIVED, source)["identity_key"]
        == result["identity_key"]
    )


@pytest.mark.parametrize(
    "minutes,fee,usable",
    [("30", "0", True), ("", "0", False), ("0", "1000", False), ("10", "NaN", False)],
)
def test_fee_pair_validity_does_not_turn_zero_or_unknown_into_free_parking(minutes, fee, usable):
    result = normalize_attributes(
        {"PRK_HM": minutes, "PRK_CRG": fee, "CHGD_FREE_SE": "Y", "DLY_MAX_CRG": "0"},
        RECEIVED,
        SOURCE,
    )
    assert result["fees"]["base_usable"] is usable
    assert result["fees"]["base_fee_krw"] == (Decimal(fee) if fee != "NaN" else None)
    assert result["fees"]["daily_max_krw"] == 0
    assert result["conditions"]["is_paid"] is True


@pytest.mark.parametrize(
    "start,end,state,offset",
    [
        ("0900", "1800", "valid", 0),
        (900, 1800.0, "valid", 0),
        ("0000", "2400", "valid", 1),
        ("2300", "2400", "valid", 1),
        ("2561", "1800", "invalid", None),
        ("0900", "1260", "invalid", None),
        ("2400", "0800", "invalid", None),
        (True, "1800", "invalid", None),
        ("09:00", "18:00", "invalid", None),
        ("090000", "180000", "invalid", None),
        ("-100", "1800", "invalid", None),
        (None, "1800", "missing", None),
        ("", "", "missing", None),
        ("0000", "0000", "unknown", None),
        ("2200", "0600", "unknown", None),
    ],
)
def test_hhmm_range_and_overnight_evidence(start, end, state, offset):
    result = operating_hours("weekend", start, end)
    assert (result["day_type"], result["state"], result["end_day_offset"]) == (
        "weekend",
        state,
        offset,
    )
    if offset == 1:
        assert result["closes_at"] == "00:00"


def test_weekend_and_holiday_stay_typed_instead_of_legacy_strings():
    row = {
        "PKLT_CD": "1",
        "PKLT_NM": "주차장",
        "LAT": 37.56,
        "LOT": 126.97,
        "WD_OPER_BGNG_TM": "2561",
        "WD_OPER_END_TM": "1800",
        "WE_OPER_BGNG_TM": "0900",
        "WE_OPER_END_TM": "1800",
        "LHLDY_OPER_BGNG_TM": "1000",
        "LHLDY_OPER_END_TM": "1600",
    }
    result = normalize_parking(row, RECEIVED, MUNICIPAL_PARKING_SOURCE)
    hours = {period["day_type"]: period for period in result["operating_hours"]}
    assert hours["weekend"]["opens_at"] == "09:00"
    assert hours["holiday"]["opens_at"] == "10:00"
    saved = parking_fields(result)
    assert not {"weekday_hours", "saturday_hours", "sunday_holiday_hours"} & saved.keys()
    assert saved["hours_notes"] is None


def municipal(**updates):
    return {
        "TPKCT": "100",
        "NOW_PRK_VHCL_CNT": "70",
        "PRK_STTS_YN": "1",
        "NOW_PRK_VHCL_UPDT_TM": "2026-11-04 08:00",
        **updates,
    }


@pytest.mark.parametrize(
    "updates,state,reason",
    [
        ({"PRK_STTS_YN": None}, "unknown", "UNKNOWN_PROVISION"),
        ({"PRK_STTS_YN": "0", "PRK_STTS_NM": "미연계중"}, "not_provided", "NOT_PROVIDED"),
        ({"PRK_STTS_YN": "?"}, "unknown", "UNKNOWN_PROVISION"),
        ({"NOW_PRK_VHCL_CNT": "100.1"}, "provided", "INVALID_OR_MISSING_COUNT"),
        ({"NOW_PRK_VHCL_CNT": "101"}, "provided", "COUNT_EXCEEDS_CAPACITY"),
        ({"NOW_PRK_VHCL_CNT": "-1"}, "provided", "INVALID_OR_MISSING_COUNT"),
        ({"TPKCT": True}, "provided", "INVALID_OR_MISSING_COUNT"),
        ({"NOW_PRK_VHCL_UPDT_TM": ""}, "provided", "MISSING_VALUE"),
        ({"NOW_PRK_VHCL_UPDT_TM": "bad"}, "provided", "INVALID_TIMESTAMP"),
        ({"NOW_PRK_VHCL_UPDT_TM": "2026-11-04 10:00"}, "provided", "FUTURE_TIMESTAMP"),
    ],
)
def test_unusable_municipal_observations_do_not_produce_calculated_vacancy(updates, state, reason):
    result = normalize_attributes(municipal(**updates), RECEIVED, MUNICIPAL_PARKING_SOURCE)[
        "availability"
    ]
    assert not result["usable"]
    assert (result["provision_state"], result["reason_code"]) == (state, reason)
    assert result["calculated_available_spaces"] is None


def test_reported_and_calculated_spaces_and_their_times_stay_separate():
    measured = normalize_attributes(
        municipal(NOW_PRK_VHCL_CNT="100"), RECEIVED, MUNICIPAL_PARKING_SOURCE
    )
    assert measured["availability"]["calculated_available_spaces"] == 0
    assert measured["availability"]["reported_available_spaces"] is None
    assert measured["availability"]["usable"]
    row = {
        "CPCTY": "100",
        "CUR_PRK_CNT": "7",
        "CUR_PRK_YN": "Y",
        "CUR_PRK_TIME": "2026-11-04 08:00",
    }
    city = normalize_attributes(row, RECEIVED, CITYDATA_PARKING_SOURCE)
    assert city["availability"]["reported_available_spaces"] == 7
    assert city["availability"]["calculated_available_spaces"] is None
    assert city["availability"]["usable"]
    assert city["source_updated_at"] is measured["source_updated_at"] is None
    assert city["operating_hours"] == []
    assert city["availability"]["received_at"] == RECEIVED
    unavailable = normalize_attributes(
        {**row, "CUR_PRK_CNT": "0", "CUR_PRK_YN": "N"}, RECEIVED, CITYDATA_PARKING_SOURCE
    )
    assert not unavailable["availability"]["usable"]
    assert unavailable["availability"]["provision_state"] == "not_provided"


def test_registration_timestamp_and_conditions_are_preserved_without_inventing_live_counts():
    row = {
        "LAST_DATA_SYNC_TM": "2026-11-03 12:00",
        "PRK_NOW_INFO_PVSN_YN": "1",
        "PKLT_KND": "NW",
        "PKLT_KND_NM": "노외 주차장",
        "OPER_SE": "1",
        "OPER_SE_NM": "시간제 주차장",
        "TELNO": "02-123",
        "CHGD_FREE_SE": "?",
        "CHGD_FREE_NM": "조건 확인 필요",
        "SAT_CHGD_FREE_SE": "N",
        "LHLDY_YN": "Y",
        "NGHT_FREE_OPN_YN": "N",
        "CRB_PKLT_MNG_GROUP_NO": "001",
    }
    result = normalize_attributes(row, RECEIVED, SOURCE)
    assert result["source_updated_at"].isoformat() == "2026-11-03T12:00:00+09:00"
    assert result["availability"] is None
    assert result["conditions"]["management_group_id"] == "001"
    assert result["conditions"]["operation_code"] == "1"
    assert result["conditions"]["phone"] == "02-123"
    assert result["conditions"]["is_paid"] is None
    assert result["conditions"]["payment_name"] == "조건 확인 필요"
    assert result["conditions"]["saturday_is_paid"] is False
    assert result["conditions"]["holiday_is_paid"] is True
