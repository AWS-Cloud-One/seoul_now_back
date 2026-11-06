from copy import deepcopy
from datetime import timedelta

import pytest

from app.modules.data_pipeline.normalizers.sdot import sdot_snapshot, sensor_code, sensor_locations
from tests.fixtures.sdot import collected, location, observation, sensor_file


def snapshot(rows, *, locations=None, receipt="2026-11-04T01:00:00+00:00"):
    return sdot_snapshot(sensor_locations(sensor_file(locations)), collected(rows, receipt))


@pytest.mark.parametrize("bad", [True, False, 1.9, 1.0, "1.0", "-1", "0", "1e3", "NaN", None, []])
def test_identifier_does_not_truncate_or_coerce(bad):
    assert sensor_code(bad) is None


def test_headers_resolve_by_name_and_preserve_device_identity():
    file = sensor_file([location("0001")])
    reordered = {"X": "B", "Z": "C", "Y": "D", "M": "E", "N": "F"}
    file["header"] = {a: file["header"][b] for a, b in reordered.items()}
    file["rows"] = [{a: file["rows"][0][b] for a, b in reordered.items()}]
    result = sensor_locations(file)
    assert result["locations"]["1"]["provider_sensor_id"] == "0001"
    assert result["locations"]["1"]["device_serial"] == "device-0001"
    file["header"]["Q"] = "위도"
    with pytest.raises(ValueError, match="invalid_sensor_file_header"):
        sensor_locations(file)


@pytest.mark.parametrize(
    "change", [{"E": "37.6"}, {"C": "different"}, {"E": "NaN"}, {"E": True}, {"F": "181"}]
)
def test_same_code_conflict_or_invalid_variant_is_never_last_row_wins(change):
    rows = [location(), location(**change)]
    for selected in (rows, rows[::-1]):
        result = sensor_locations(sensor_file(selected))
        assert result["locations"] == {}
        assert result["quality"]["rejected"] == 2


def test_equal_location_duplicates_keep_evidence():
    result = sensor_locations(sensor_file([location(), location()]))
    assert result["quality"]["accepted"] == 1
    assert result["quality"]["duplicates"] == 1
    assert result["locations"]["1"]["source_rows"] == [0, 1]


@pytest.mark.parametrize("count", [True, 1.9, "1.9", "-1", "NaN", None, 9223372036854775808])
def test_counts_reject_bad_types_without_becoming_zero(count):
    result = snapshot([observation(VISITOR_COUNT=count)])
    assert result["items"][0]["count_10min"] is None
    assert result["items"][0]["observation_status"] == "invalid"
    assert result["observation_quality"]["rejected"] == 1


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"SENSING_TIME": 123}, "INVALID_TIMESTAMP"),
        ({"SENSING_TIME": "2026-11-05 09:00"}, "FUTURE_TIMESTAMP"),
        ({"DATE": "2026-11-05 09:00"}, "FUTURE_TIMESTAMP"),
    ],
)
def test_invalid_and_future_times_excluded(changes, reason):
    result = snapshot([observation(**changes)])
    assert result["items"][0]["count_10min"] is None
    assert reason in result["input_rows"][0]["field_issues"].values()


def test_actual_instant_orders_offsets_and_does_not_replace_them():
    from app.modules.data_pipeline.normalizers.values import timestamp

    rows = [
        observation(SENSING_TIME="2026-11-04T09:00:00+09:00"),
        observation(
            SENSING_TIME="2026-11-04T00:30:00+00:00",
            DATE="2026-11-04T00:31:00+00:00",
            VISITOR_COUNT="9",
        ),
    ]
    result = snapshot(rows)
    item = result["items"][0]
    assert item["count_10min"] == 9
    assert timestamp(item["observed_at"]).value.utcoffset() == timedelta(0)
    assert item["provider_collected_at"].endswith("+00:00")


def test_zero_unobserved_conflict_and_data_no_are_separate():
    rows = [
        observation("1"),
        observation("2", VISITOR_COUNT="3"),
        observation("2", VISITOR_COUNT="4"),
        observation("3"),
        observation("3", DATA_NO="2", SENSING_TIME="2026-11-04 09:30", DATE="2026-11-04 09:31"),
    ]
    result = snapshot(rows, locations=[location(str(i)) for i in range(1, 5)])
    items = {i["id"]: i for i in result["items"]}
    assert items["1"]["count_10min"] == 0 and items["1"]["usable"]
    assert items["2"]["observation_status"] == "conflicting"
    assert items["3"]["observation_status"] == "valid"
    assert items["3"]["data_no"] == "2"
    assert items["4"]["observation_status"] == "unobserved"
    assert result["observation_quality"]["rejected"] == 2
    assert result["observation_quality"]["accepted"] == 3


def test_old_conflict_does_not_invalidate_new_observation_and_equal_rows_deduplicate():
    rows = [
        observation(VISITOR_COUNT="2"),
        observation(VISITOR_COUNT="3"),
        observation(SENSING_TIME="2026-11-04 09:10", DATE="2026-11-04 09:11", VISITOR_COUNT="4"),
    ]
    rows.append(deepcopy(rows[-1]))
    for selected in (rows, rows[::-1]):
        result = snapshot(selected)
        assert result["items"][0]["count_10min"] == 4
        assert result["observation_quality"]["duplicates"] == 1
        assert result["observation_quality"]["rejected"] == 2


def test_missing_receipt_or_provider_date_remains_unverified():
    result = snapshot([observation(DATE=None)], receipt=None)
    assert result["items"][0]["count_10min"] == 0
    assert result["items"][0]["usable"] is False
    assert result["input_rows"][0]["received_at"] is None
    assert result["complete_history"] is False
    assert result["latest_all_sensors_verified"] is False


def test_latest_provider_date_is_selected_without_changing_sensor_time():
    result = snapshot([observation(), observation(DATE="2026-11-04 09:02")])
    item = result["items"][0]
    assert item["count_10min"] == 0
    assert item["provider_collected_at"] == "2026-11-04T00:02:00+00:00" and item["usable"]
    assert item["observed_at"] == "2026-11-04T00:00:00+00:00"
    assert len(result["input_rows"]) == 2


def test_provider_date_orders_corrections_without_ranking_data_no():
    rows = [
        observation(DATA_NO="9", VISITOR_COUNT="15"),
        observation(DATA_NO="1", VISITOR_COUNT="4", DATE="2026-11-04 09:02"),
    ]
    for selected in (rows, rows[::-1]):
        result = snapshot(selected)
        item = result["items"][0]
        assert item["usable"] and item["count_10min"] == 4 and item["data_no"] == "1"
        assert result["observation_quality"]["rejected"] == 0
        assert sum(r["selection"] == "superseded" for r in result["input_rows"]) == 1


def test_equal_clock_conflict_is_held_but_equivalent_tags_are_not():
    result = snapshot([observation(DATA_NO="1"), observation(DATA_NO="2", VISITOR_COUNT="7")])
    assert result["items"][0]["observation_status"] == "conflicting"
    assert not result["items"][0]["usable"]
    for rows in (
        [observation(DATA_NO="1"), observation(DATA_NO="2")],
        [observation(DATA_NO="2"), observation(DATA_NO="1")],
    ):
        item = snapshot(rows)["items"][0]
        assert item["usable"] and item["count_10min"] == 0
        assert item["data_no_variants"] == ["1", "2"] and item["data_no"] == "1"
