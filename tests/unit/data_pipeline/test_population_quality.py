import pytest

from app.modules.data_pipeline.normalizers.storage_fields import population


def data(**updates):
    return {
        "AREA_CD": "POI001",
        "AREA_NM": "test",
        "LIVE_PPLTN_STTS": [
            {
                "AREA_CD": "POI001",
                "AREA_NM": "test",
                "AREA_PPLTN_MIN": "0",
                "AREA_PPLTN_MAX": "10",
                "AREA_CONGEST_LVL": "여유",
                "REPLACE_YN": "N",
                "PPLTN_TIME": "2026-11-04 09:00",
                **updates,
            }
        ],
    }


@pytest.mark.parametrize(
    "updates,field,reason",
    [
        ({"AREA_CONGEST_LVL": []}, "congestion", "UNKNOWN_CONGESTION"),
        ({"AREA_CONGEST_LVL": "new"}, "congestion", "UNKNOWN_CONGESTION"),
        ({"REPLACE_YN": "maybe"}, "is_replacement", "INVALID_FLAG"),
        ({"REPLACE_YN": None}, "is_replacement", "MISSING_VALUE"),
        ({"PPLTN_TIME": None}, "observed_at", "MISSING_VALUE"),
        ({"AREA_PPLTN_MAX": 1.5}, "max_people", "INVALID_INTEGER"),
        ({"AREA_PPLTN_MAX": True}, "max_people", "INVALID_NUMBER"),
        ({"AREA_PPLTN_MAX": 2**31}, "max_people", "INTEGER_OUT_OF_RANGE"),
    ],
)
def test_field_quality_is_not_hidden_by_an_existing_range(updates, field, reason):
    result = population(data(**updates))
    assert result["min_people"] == 0
    assert result["quality"]["section_status"] == "partial"
    assert result["quality"]["field_issues"][field] == reason
    assert result["reason_code"] is not None


def test_reversed_range_is_invalid_and_replacement_is_preserved():
    result = population(data(AREA_PPLTN_MIN="11"))
    assert result["min_people"] is None and result["max_people"] is None
    assert result["quality"]["error_code"] == "invalid_or_missing_population_range"
    result = population(data(REPLACE_YN="Y"))
    assert result["is_replacement"] is True
    assert result["quality"]["section_status"] == "usable"
