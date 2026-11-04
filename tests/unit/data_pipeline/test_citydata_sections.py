from copy import deepcopy

import pytest

from app.modules.data_pipeline.normalizers.citydata_sections import select_citydata
from app.modules.data_pipeline.normalizers.parking import CITYDATA_PARKING_SOURCE, normalize_parking
from app.modules.data_pipeline.normalizers.road_geometry import citydata_geometry_index
from tests.unit.data_pipeline.test_citydata import city


@pytest.mark.parametrize(
    "key,section", [("PRK_STTS", "parking"), ("ROAD_TRAFFIC_STTS", "road_geometry")]
)
def test_missing_empty_invalid_and_usable_sections(key, section):
    data = city()
    data.pop(key, None)
    assert select_citydata(data)["sections"][section]["state"] == "missing"
    data[key] = [] if section == "parking" else {key: []}
    assert select_citydata(data)["sections"][section]["state"] == "empty"
    for invalid in (None, "", 0, [None], [{}]):
        data[key] = invalid if section == "parking" else {key: invalid}
        assert select_citydata(data)["sections"][section]["state"] == "invalid"


def test_selection_removes_unused_content_but_preserves_all_consumed_facts():
    data = city()
    parking = {
        "PRK_CD": "1",
        "PRK_NM": "p",
        "LAT": "37.5",
        "LNG": "127",
        "CPCTY": "100",
        "CUR_PRK_CNT": "0",
        "CUR_PRK_YN": "Y",
        "CUR_PRK_TIME": "2020-01-01 09:00",
        "RATES": "100",
        "TIME_RATES": "5",
        "ADD_RATES": "50",
        "ADD_TIME_RATES": "10",
        "PRK_TYPE": "public",
        "PAY_YN": "Y",
        "UNUSED": ["private"],
    }
    data["PRK_STTS"] = [parking]
    data["ROAD_TRAFFIC_STTS"] = {
        "ROAD_TRAFFIC_STTS": [{"LINK_ID": "1", "XYLIST": "127_37.5|127.1_37.6", "SPD": "50"}],
        "AVG_ROAD_DATA": {"ROAD_TRAFFIC_TIME": "2020-01-01 09:00", "ROAD_TRAFFIC_SPD": "40"},
    }
    original = deepcopy(data)
    selection = select_citydata(data)
    selected = selection["CITYDATA"]
    assert data == original
    assert "UNSELECTED_SECTION" not in selected
    assert selection["unknown_keys"] == ["UNSELECTED_SECTION"]
    assert "UNUSED" not in selected["PRK_STTS"][0]
    assert normalize_parking(parking, None, CITYDATA_PARKING_SOURCE) == normalize_parking(
        selected["PRK_STTS"][0], None, CITYDATA_PARKING_SOURCE
    )
    assert citydata_geometry_index([{"data": data}]) == citydata_geometry_index(
        [{"data": selected}]
    )
    assert "ROAD_TRAFFIC_SPD" not in selected["ROAD_TRAFFIC_STTS"]["AVG_ROAD_DATA"]


def test_invalid_candidate_values_do_not_become_usable_sections():
    data = city()
    data["PRK_STTS"] = [{"PRK_CD": "bad", "LAT": "NaN"}]
    data["ROAD_TRAFFIC_STTS"] = {"ROAD_TRAFFIC_STTS": [{"LINK_ID": "1", "XYLIST": "bad"}]}
    result = select_citydata(data)
    for name in ("parking", "road_geometry"):
        assert result["sections"][name]["state"] == "invalid"
        assert result["sections"][name]["row_validation"]["rejected"] == 1
