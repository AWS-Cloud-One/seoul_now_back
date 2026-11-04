from copy import deepcopy
from datetime import UTC, datetime, timedelta
from itertools import permutations

import pytest

from app.modules.data_pipeline.normalizers.parking import CITYDATA_PARKING_SOURCE, normalize_parking
from app.modules.data_pipeline.normalizers.parking_resolution import (
    ParkingCandidate,
    resolve_parking_identities,
    select_parking_candidates,
)

NOW = datetime(2026, 11, 4, 0, 0, tzinfo=UTC)


def candidate(origin="a", receipt=NOW, **updates):
    record = normalize_parking(
        {
            "PRK_CD": "1",
            "PRK_NM": "주차장",
            "ADDRESS": "서울 주소",
            "LAT": 37.56,
            "LNG": 126.97,
            "CPCTY": "100",
            "CUR_PRK_CNT": "10",
            "CUR_PRK_YN": "Y",
            "CUR_PRK_TIME": "2026-11-04 08:00",
            "RATES": "1000",
            "TIME_RATES": "30",
            **updates,
        },
        receipt,
        CITYDATA_PARKING_SOURCE,
    )
    return ParkingCandidate(record, "payload-" + origin, receipt, origin)


def old_record(item, rid="first", **updates):
    row = item.record
    return dict(
        source=row["source"],
        provider_record_id=row["source_id"],
        name=row["name"],
        address=row["address"],
        position=row["position"],
        legacy_record_id=row["identity_key"],
        source_record_id=rid,
        **updates,
    )


def test_exact_duplicates_use_stable_provenance_and_do_not_mutate_input():
    items = [candidate("a"), candidate("b", receipt=NOW + timedelta(minutes=1)), candidate("c")]
    original = deepcopy(items)
    for ordering in permutations(items):
        selected, duplicates, reasons = select_parking_candidates(ordering)
        assert selected == [items[1]]
        assert duplicates == 2 and not reasons
    assert items == original


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"RATES": "2000"}, "conflicting_parking_registration"),
        ({"RATES": "2000", "CUR_PRK_TIME": "2026-11-04 08:30"}, "conflicting_parking_registration"),
        ({"PRK_NM": "다른 이름"}, "conflicting_parking_registration"),
        ({"CUR_PRK_CNT": "11"}, "conflicting_parking_observation"),
        ({"CUR_PRK_CNT": "11", "CUR_PRK_TIME": ""}, "conflicting_parking_observation"),
    ],
)
def test_conflicts_are_held_in_every_order_and_do_not_hide_as_duplicates(changes, reason):
    first, other = candidate("a"), candidate("b", **changes)
    for items in permutations([first, first, other]):
        selected, duplicates, reasons = select_parking_candidates(items)
        assert not selected and duplicates == 1 and reasons == {reason: 2}


def test_only_live_changes_can_use_observation_time_and_older_rows_are_not_duplicates():
    old = candidate("a")
    fresh = candidate("b", CUR_PRK_TIME="2026-11-04 08:30", CUR_PRK_CNT="0")
    for items in permutations([old, fresh]):
        selected, duplicates, reasons = select_parking_candidates(items)
        assert selected == [fresh] and duplicates == 0
        assert reasons == {"older_parking_observation": 1}


def test_same_provider_code_at_distinct_positions_is_not_merged():
    first, second = candidate("a"), candidate("b", LAT=37.57)
    selected, duplicates, reasons = select_parking_candidates([first, second])
    assert len(selected) == 2 and duplicates == 0 and not reasons
    resolved, reasons = resolve_parking_identities([item.record for item in selected], [])
    assert len(resolved) == 2 and all(old is None for _, old in resolved) and not reasons


@pytest.mark.parametrize("changes", [{"PRK_NM": "새 이름"}, {"ADDRESS": "새 주소"}, {"LAT": 37.57}])
def test_name_address_and_coordinate_corrections_keep_unique_existing_record(changes):
    baseline = candidate()
    old = old_record(baseline)
    changed = candidate(**changes)
    resolved, reasons = resolve_parking_identities([changed.record], [old])
    assert resolved == [(changed.record, old)] and not reasons


def test_unknown_relocation_and_competing_claims_are_held_before_writes():
    baseline = candidate()
    old = old_record(baseline)
    changed = candidate(PRK_NM="새 이름", ADDRESS="새 주소", LAT=37.57)
    assert resolve_parking_identities([changed.record], [old]) == (
        [],
        {"identity_change_requires_review": 1},
    )
    relocated = candidate(LAT=37.57)
    for items in permutations([baseline.record, relocated.record]):
        assert resolve_parking_identities(items, [old]) == ([], {"ambiguous_parking_identity": 2})


def test_conflicting_identity_anchors_and_other_sources_do_not_get_merged():
    first, second = candidate(), candidate(PRK_NM="other", ADDRESS="other", LAT=37.57)
    known = [old_record(first), old_record(second, rid="second")]
    relocated = candidate(LAT=37.57)  # Name/address point to first; position points to second.
    assert resolve_parking_identities([relocated.record], known) == (
        [],
        {"ambiguous_parking_identity": 1},
    )
    different_source = dict(first.record, source="seoul_getparkinfo")
    assert resolve_parking_identities([different_source], known) == ([(different_source, None)], {})


def test_complementary_citydata_rows_merge_with_original_field_and_live_evidence():
    registration = candidate(
        "registration",
        receipt=NOW + timedelta(minutes=10),
        CUR_PRK_YN="N",
        CUR_PRK_CNT="",
        CUR_PRK_TIME="",
        ROAD_ADDR="",
    )
    current = candidate(
        "current",
        PRK_NM="주차장 현황 표기",
        ROAD_ADDR="서울 도로명주소",
        TIME_RATES=None,
        CUR_PRK_CNT="0",
    )
    original = deepcopy([registration, current])
    expected = None
    for ordering in permutations(original):
        selected, duplicates, reasons = select_parking_candidates(ordering)
        assert len(selected) == 1 and duplicates == 1 and not reasons
        merged = selected[0]
        assert merged.record["name"] == "주차장"
        assert merged.record["address"] == "서울 도로명주소"
        assert merged.record["fees"]["base_minutes"] == 30
        assert merged.record["fees"]["base_fee_krw"] == 1000
        assert merged.record["fees"]["base_usable"]
        assert "fees.base_minutes" not in merged.record["field_issues"]
        assert merged.record["availability"]["reported_available_spaces"] == 0
        assert merged.record["availability"]["received_at"] == NOW
        assert merged.observation == current
        sources = merged.selection["inputs"]
        assert sources[merged.selection["field_sources"]["TIME_RATES"]]["origin"] == "registration"
        assert sources[merged.selection["field_sources"]["ROAD_ADDR"]]["origin"] == "current"
        assert sources[merged.selection["observation_source"]]["payload_id"] == current.payload_id
        assert expected is None or merged == expected
        expected = merged
    assert [registration, current] == original


@pytest.mark.parametrize(
    "changes",
    [
        {"CPCTY": "101"},
        {"CPCTY": "-1"},
        {"RATES": "2000"},
        {"RATES": "invalid"},
        {"ADDRESS": "실제로 다른 지번"},
        {"PAY_YN": "N"},
    ],
)
def test_complementary_rows_do_not_hide_known_contradictions_or_invalid_values(changes):
    registration = candidate(
        "registration", CUR_PRK_YN="N", CUR_PRK_CNT="", CUR_PRK_TIME="", PAY_YN="Y"
    )
    current = candidate("current", TIME_RATES=None, **changes)
    for items in permutations([registration, current]):
        selected, duplicates, reasons = select_parking_candidates(items)
        assert not selected and duplicates == 0
        assert reasons == {"conflicting_parking_registration": 2}


def test_complementary_fee_components_are_not_invented_into_a_price_pair():
    registration = candidate("registration", CUR_PRK_YN="N", RATES=None)
    current = candidate("current", TIME_RATES=None)
    selected, _, reasons = select_parking_candidates([registration, current])
    assert not selected and reasons == {"conflicting_parking_registration": 2}


def test_complementary_missing_values_preserve_real_zero_capacity_and_price():
    registration = candidate(
        "registration", CUR_PRK_YN="N", CPCTY=None, RATES=None, TIME_RATES=None
    )
    current = candidate("current", CPCTY="0", CUR_PRK_CNT="0", RATES="0")
    selected, _, reasons = select_parking_candidates([registration, current])
    assert not reasons
    assert selected[0].record["registered_capacity"] == 0
    assert selected[0].record["fees"]["base_fee_krw"] == 0
    assert selected[0].record["fees"]["base_usable"]
    assert selected[0].record["availability"]["usable"]


def test_complementary_rows_keep_true_live_conflicts_and_choose_newer_observations():
    registration = candidate("registration", CUR_PRK_YN="N")
    first = candidate("first", TIME_RATES=None)
    conflicting = candidate("conflict", TIME_RATES=None, CUR_PRK_CNT="11")
    for items in permutations([registration, first, conflicting]):
        selected, _, reasons = select_parking_candidates(items)
        assert not selected and reasons == {"conflicting_parking_observation": 3}
    newer = candidate("newer", TIME_RATES=None, CUR_PRK_TIME="2026-11-04 08:30", CUR_PRK_CNT="0")
    for items in permutations([registration, first, newer]):
        selected, duplicates, reasons = select_parking_candidates(items)
        assert not reasons and duplicates == 2
        assert selected[0].observation == newer


def test_registration_merge_does_not_cross_source_or_position_boundaries():
    registration = candidate("registration", CUR_PRK_YN="N")
    current = candidate("current", TIME_RATES=None, LAT=37.57)
    selected, duplicates, reasons = select_parking_candidates([registration, current])
    assert len(selected) == 2 and duplicates == 0 and not reasons
    current = candidate("current", TIME_RATES=None)
    current.record["source"] = "another_source"
    selected, duplicates, reasons = select_parking_candidates([registration, current])
    assert len(selected) == 2 and duplicates == 0 and not reasons


def test_observation_time_is_validated_against_its_own_receipt():
    registration = candidate("registration", CUR_PRK_YN="N")
    current = candidate(
        "current",
        receipt=NOW + timedelta(minutes=10),
        TIME_RATES=None,
        CUR_PRK_TIME="2026-11-04 09:05",
    )
    selected, _, reasons = select_parking_candidates([registration, current])
    assert not reasons and selected[0].received_at == NOW
    assert selected[0].record["availability"]["usable"]
    assert "availability.observed_at" not in selected[0].record["field_issues"]
    future = candidate("future", TIME_RATES=None, CUR_PRK_TIME="2026-11-04 09:05")
    selected, _, reasons = select_parking_candidates([registration, future])
    assert not reasons and not selected[0].record["availability"]["usable"]
    assert selected[0].record["field_issues"]["availability.observed_at"] == "FUTURE_TIMESTAMP"
