"""Traffic field validity is independent of source timestamps and write freshness."""

from decimal import Decimal

import pytest

from app.modules.data_pipeline.normalizers.storage_fields import road_traffic_fields


@pytest.mark.parametrize("value", [0, "0", "0.0", Decimal("0"), " 12.5 "])
def test_valid_numbers_and_zero_remain_values(value):
    result = road_traffic_fields({"prcs_spd": value, "prcs_trv_time": "0"})
    assert result["value_status"] == "valid" and result["speed_kmh"] == Decimal(str(value).strip())
    assert result["travel_seconds"] == 0 and result["field_issues"] == {}
    assert result["observed_at"] is None and result["reason_code"] == "SOURCE_TIME_UNKNOWN"


@pytest.mark.parametrize(
    "value", ["NaN", "Infinity", "-Infinity", -1, "-2", True, [], {}, "broken", float("nan")]
)
def test_one_invalid_metric_does_not_invalidate_or_zero_the_other(value):
    result = road_traffic_fields({"prcs_spd": value, "prcs_trv_time": "0"})
    assert result["value_status"] == "partial"
    assert result["speed_kmh"] is None and result["travel_seconds"] == 0
    assert result["field_issues"] == {"speed_kmh": "INVALID_NUMBER"}
    assert result["value_counts"] == {"valid": 1, "invalid": 1, "missing": 0}


@pytest.mark.parametrize(
    "row,state,issues",
    [
        ({}, "missing", {"speed_kmh": "MISSING_VALUE", "travel_seconds": "MISSING_VALUE"}),
        (
            {"prcs_spd": "", "prcs_trv_time": " "},
            "missing",
            {"speed_kmh": "MISSING_VALUE", "travel_seconds": "MISSING_VALUE"},
        ),
        (
            {"prcs_spd": "NaN", "prcs_trv_time": "-1"},
            "invalid",
            {"speed_kmh": "INVALID_NUMBER", "travel_seconds": "INVALID_NUMBER"},
        ),
        (
            {"prcs_spd": "NaN"},
            "invalid",
            {"speed_kmh": "INVALID_NUMBER", "travel_seconds": "MISSING_VALUE"},
        ),
    ],
)
def test_all_missing_and_invalid_have_separate_reasons(row, state, issues):
    result = road_traffic_fields(row)
    assert result["value_status"] == state and result["field_issues"] == issues
    assert result["speed_kmh"] is result["travel_seconds"] is None
    assert result["reason_code"] != "SOURCE_TIME_UNKNOWN"


def test_provider_or_citydata_times_cannot_be_injected_into_topis_observed_time():
    result = road_traffic_fields(
        {
            "prcs_spd": "1",
            "prcs_trv_time": "2",
            "observed_at": "2026-01-01T00:00:00Z",
            "ROAD_TRAFFIC_TIME": "2026-01-01 09:00",
        }
    )
    assert result["observed_at"] is None


@pytest.mark.parametrize(
    "data", [None, {}, {"rows": []}, {"rows": [None]}, {"rows": [{"link_id": "other"}]}]
)
def test_unusable_response_shape_has_no_buffered_values(data):
    from app.modules.data_pipeline.normalizers.topis import traffic_observation

    result = traffic_observation("r1", data, None)
    assert result["values"] is None and result["error_code"] == "invalid_link_response"


def test_only_typed_fields_enter_buffer_and_missing_receipt_is_not_fabricated():
    from datetime import UTC, datetime

    from app.modules.data_pipeline.normalizers.topis import traffic_observation

    receipt = datetime(2026, 11, 4, tzinfo=UTC)
    data = {"rows": [{"link_id": "r1", "prcs_spd": "0", "prcs_trv_time": "2", "unused": "discard"}]}
    result = traffic_observation("r1", data, receipt)
    assert result["values"] == {
        "speed_kmh": 0,
        "travel_seconds": 2,
        "observed_at": None,
        "reason_code": "SOURCE_TIME_UNKNOWN",
        "received_at": receipt,
    }
    assert traffic_observation("r1", data, None)["error_code"] == "missing_traffic_receipt"
    data["rows"][0].update(prcs_spd="invalid", prcs_trv_time="")
    invalid = traffic_observation("r1", data, None)
    assert invalid["status"] == "failed" and invalid["error_code"] == "invalid_traffic_values"
