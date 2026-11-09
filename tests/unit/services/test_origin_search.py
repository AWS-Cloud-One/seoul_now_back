import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest

from app.clients.kakao import KakaoConfigurationError, KakaoError, KakaoLocalClient
from app.core.exceptions import ServiceError
from app.schemas.origin import OriginHint
from app.schemas.recommendations import ConditionsPatch
from app.services.origin_search import OriginSearchService
from tests.fixtures.origin import ORIGIN_TOKEN


def poi(identifier, name="시청역", address="서울 중구", category="SW8", **extra):
    return {
        "id": str(identifier),
        "place_name": name,
        "address_name": address,
        "x": str(126.97 + int(identifier) / 1000),
        "y": "37.56",
        "category_group_code": category,
        "category_name": {"SW8": "교통 > 지하철역", "CE7": "음식점 > 카페"}.get(category),
    } | extra


def page(rows=(), *, more=False):
    return {"documents": list(rows), "meta": {"is_end": not more}}


def region(province="서울특별시", district="중구"):
    return {
        "documents": [
            {
                "region_type": "B",
                "region_1depth_name": province,
                "region_2depth_name": district,
                "region_3depth_name": "",
            }
        ]
    }


def provider_for(*rows):
    provider = AsyncMock()
    provider.keyword.return_value = page(rows)
    provider.address.return_value = page()
    by_point = {(float(r["y"]), float(r["x"])): r.get("address_name") or "서울 중구" for r in rows}

    async def lookup(lat, lon):
        address = by_point.get((lat, lon), "서울 중구").split()
        provinces = {
            "서울": "서울특별시",
            "서울특별시": "서울특별시",
            "인천": "인천광역시",
            "부산": "부산광역시",
            "경기": "경기도",
        }
        return region(provinces.get(address[0], address[0]), address[1] if len(address) > 1 else "")

    provider.region.side_effect = lookup
    return provider


def hint(text, *, kind="region", source="message", message_index=None):
    return OriginHint(
        kind=kind,
        text=text,
        quote=text,
        source=source,
        message_index=message_index,
    )


def search(provider, name="시청역", hints=()):
    return asyncio.run(OriginSearchService(provider).search(name, hints))


def ids(result):
    return [c.provider_id for c in result.candidates]


def clarification(result):
    return result.clarification(
        context_token=ORIGIN_TOKEN,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        pending_patch=ConditionsPatch(max_travel_seconds=1800),
    )


def test_station_is_ranked_before_similarly_named_shops_beyond_first_five_results():
    shops = [poi(i, f"카페 시청역{i}점", category="CE7") for i in range(1, 7)]
    station = poi(7, "시청역 1호선")
    provider = provider_for(*shops, station)
    result = search(provider)
    assert ids(result)[0] == "7"
    assert result.mode == "confirm" and result.search_limited
    assert result.candidates[0].match.name == "normalized"
    assert result.candidates[0].provider_order == 6
    assert provider.keyword.await_count == 1 and provider.region.await_count == 5
    question = clarification(result)
    assert (
        question.origin_context.preferred_candidate_id == question.choices[0].value["candidate_id"]
    )
    assert question.pending_patch.model_dump(exclude_unset=True) == {"max_travel_seconds": 1800}
    assert "origin" not in question.pending_patch.model_fields_set


def test_equal_evidence_preserves_provider_order_without_claiming_preferred_candidate():
    provider = provider_for(poi(2, address="인천 남동구"), poi(1))
    result = search(provider)
    assert ids(result) == ["2", "1"] and result.mode == "choose"
    assert clarification(result).origin_context.preferred_candidate_id is None
    assert not result.search_limited


def test_single_candidate_still_requires_confirmation():
    result = search(provider_for(poi(1)))
    assert result.mode == "confirm"
    assert len(clarification(result).choices) == 1


def test_exact_name_is_not_outranked_by_an_unrelated_station_based_on_suffix_alone():
    provider = provider_for(poi(1, "커피역", category="CE7"), poi(2, "다른역"))
    result = search(provider, "커피역")
    assert ids(result)[0] == "1"
    assert result.mode == "confirm"


def test_explicit_region_filters_conflicts_and_overrides_old_origin_address():
    provider = provider_for(poi(1), poi(2, address="인천 남동구"))
    result = search(
        provider,
        hints=[
            hint("인천"),
            hint("서울 중구", kind="address", source="current_origin"),
        ],
    )
    assert ids(result) == ["2"]
    assert result.candidates[0].location.region == "incheon"
    provider.keyword.assert_awaited_once_with("인천 시청역", page=1, size=15)
    assert provider.region.await_count == 1


def test_latest_user_origin_context_beats_old_context_without_excluding_alternatives():
    provider = provider_for(poi(1, "시청역1호선"), poi(2, address="인천 남동구"))
    result = search(
        provider,
        hints=[
            hint("서울", source="current_origin"),
            hint("서울", source="recent_user_message", message_index=0),
            hint("인천", source="recent_user_message", message_index=2),
        ],
    )
    assert ids(result) == ["2", "1"]
    assert result.mode == "confirm"
    assert result.candidates[0].match.context_matches == ["region"]
    provider.keyword.assert_awaited_once_with("인천 시청역", page=1, size=15)


def test_new_station_name_does_not_inherit_an_old_cafe_type():
    provider = provider_for(poi(1))
    result = search(provider, hints=[hint("카페", kind="place_type", source="current_origin")])
    assert ids(result) == ["1"]
    provider.keyword.assert_awaited_once_with("시청역", page=1, size=15)


def test_province_in_search_name_is_an_explicit_constraint():
    provider = provider_for(poi(1), poi(2, address="인천 남동구"))
    result = search(provider, "인천 시청역")
    assert ids(result) == ["2"]


def test_region_alias_and_land_address_survive_road_address_display_conversion():
    row = poi(1, address="서울특별시 중구 정동", road_address_name="서울 중구 세종대로 110")
    provider = provider_for(row)
    provider.region.side_effect = None
    provider.region.return_value = {
        "documents": [
            {
                "region_type": "B",
                "region_1depth_name": "서울특별시",
                "region_2depth_name": "중구",
                "region_3depth_name": "정동",
            }
        ]
    }
    result = search(provider, hints=[hint("서울"), hint("정동")])
    candidate = result.candidates[0]
    assert candidate.location.address == "서울 중구 세종대로 110"
    assert candidate.land_address == "서울특별시 중구 정동"
    assert candidate.match.explicit_matches == ["region"]


def test_district_suffix_is_not_a_match_for_a_different_district():
    provider = provider_for(poi(1, address="인천 강서구"), poi(2, address="인천 서구"))
    result = search(provider, hints=[hint("인천"), hint("서구")])
    assert ids(result) == ["2"]


def test_province_only_region_lookup_does_not_disprove_district():
    provider = provider_for(poi(1))
    provider.region.side_effect = None
    provider.region.return_value = {
        "documents": [{"region_type": "B", "region_1depth_name": "서울특별시"}]
    }
    result = search(provider, hints=[hint("중구")])
    assert ids(result) == ["1"]
    assert "region" not in result.candidates[0].match.explicit_matches


def test_missing_dong_in_road_address_is_unknown_until_coordinate_lookup():
    row = poi(1, address=None, road_address_name="서울 중구 세종대로 110")
    provider = provider_for(row)
    provider.region.side_effect = None
    provider.region.return_value = {
        "documents": [
            {
                "region_type": "B",
                "region_1depth_name": "서울특별시",
                "region_2depth_name": "중구",
                "region_3depth_name": "정동",
            }
        ]
    }
    result = search(provider, hints=[hint("정동")])
    assert ids(result) == ["1"]
    assert result.candidates[0].match.explicit_matches == ["region"]
    assert provider.region.await_count == 1


def test_verified_conflicting_dong_excludes_candidate_without_claiming_unknown():
    row = poi(1, address=None, road_address_name="서울 중구 세종대로 110")
    provider = provider_for(row)
    provider.region.side_effect = None
    provider.region.return_value = {
        "documents": [
            {
                "region_type": "B",
                "region_1depth_name": "서울특별시",
                "region_2depth_name": "중구",
                "region_3depth_name": "명동",
            }
        ]
    }
    result = search(provider, hints=[hint("정동")])
    assert not result.candidates and result.mode == "refine"


def test_line_one_does_not_match_line_eleven():
    provider = provider_for(poi(1, "시청역 11호선"), poi(2, "시청역 1호선"))
    result = search(provider, hints=[hint("1호선", kind="station_line")])
    assert ids(result) == ["2"]


def test_explicit_branch_does_not_choose_another_branch():
    provider = provider_for(
        poi(1, "카페 홍대점", category="CE7"),
        poi(2, "카페 강남점", category="CE7"),
    )
    result = search(provider, "카페", [hint("강남점", kind="branch")])
    assert ids(result) == ["2"]


def test_explicit_place_type_excludes_conflicting_types():
    provider = provider_for(poi(1, category="CE7"), poi(2))
    result = search(provider, hints=[hint("지하철역", kind="place_type")])
    assert ids(result) == ["2"]


def test_targeted_second_search_can_find_station_omitted_from_initial_shops():
    shop, station = poi(1, "시청역점 카페", category="CE7"), poi(2)
    provider = provider_for(shop, station)
    provider.keyword.side_effect = [page([shop], more=True), page([station])]
    result = search(provider)
    assert ids(result)[0] == "2" and result.search_limited
    assert provider.keyword.call_args_list[1].kwargs == {"page": 1, "size": 15, "category": "SW8"}
    assert provider.keyword.await_count == 2
    provider.address.assert_not_awaited()


def test_empty_contextual_search_can_relax_query_but_keeps_explicit_region_constraint():
    row = poi(1, address="인천 남동구")
    provider = provider_for(row)
    provider.keyword.side_effect = [page(), page([row])]
    result = search(provider, hints=[hint("인천")])
    assert ids(result) == ["1"]
    assert provider.keyword.call_args_list[0].args == ("인천 시청역",)
    assert provider.keyword.call_args_list[1].args == ("시청역",)
    assert "region" in result.candidates[0].match.explicit_matches


def test_address_fallback_preserves_coordinates_and_distinguishes_building_numbers():
    rows = [
        {"address_name": "서울 중구 세종대로 1100", "x": "126.978", "y": "37.566"},
        {"address_name": "서울 중구 세종대로 110", "x": "126.977", "y": "37.565"},
    ]
    provider = provider_for(*rows)
    provider.keyword.return_value = page()
    provider.address.return_value = page(rows)
    result = search(
        provider, "서울 중구 세종대로 110", [hint("서울 중구 세종대로 110", kind="address")]
    )
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.source == "kakao_address" and candidate.provider_id is None
    assert candidate.location.coordinates.latitude == 37.565
    assert candidate.location.coordinates.longitude == 126.977
    assert provider.keyword.await_count + provider.address.await_count == 2


def test_missing_optional_category_and_address_remain_unknown():
    row = poi(1, address=None, category=None, category_name=None)
    provider = provider_for(row)
    result = search(provider)
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.category_code is None and candidate.location.address is None
    assert candidate.match.place_type == "unknown"
    assert "주소 미확인" in clarification(result).question
    assert provider.keyword.await_count == 2  # A bounded attempt to find classified candidates.


def test_land_address_alone_does_not_disprove_a_road_address_hint():
    row = poi(1, address="서울 중구 태평로1가 31")
    provider = provider_for(row)
    result = search(provider, hints=[hint("서울 중구 세종대로 110", kind="address")])
    assert ids(result) == ["1"]
    assert "address" not in result.candidates[0].match.explicit_matches


def test_conflicting_road_address_is_excluded_when_comparable_data_exists():
    wrong = poi(1, road_address_name="서울 중구 을지로 110")
    right = poi(2, road_address_name="서울 중구 세종대로 110")
    provider = provider_for(wrong, right)
    result = search(provider, hints=[hint("서울 중구 세종대로 110", kind="address")])
    assert ids(result) == ["2"]


def test_same_provider_identity_is_deduplicated_but_separate_exits_are_preserved():
    first, second = poi(1, "시청역 1번출구"), poi(2, "시청역 2번출구")
    provider = provider_for(first, first.copy(), second)
    result = search(provider)
    assert ids(result) == ["1", "2"] and result.mode == "choose"
    assert provider.region.await_count == 2


def test_same_coordinate_is_verified_once_without_merging_distinct_places():
    first, second = poi(1), poi(2, name="시청역 별관", x=poi(1)["x"])
    provider = provider_for(first, second)
    result = search(provider)
    assert len(result.candidates) == 2 and provider.region.await_count == 1


def test_conflicting_coordinates_for_same_provider_identity_are_not_arbitrarily_chosen():
    first = poi(1)
    conflict = first | {"x": "127.1"}
    provider = provider_for(first, conflict, poi(2))
    result = search(provider)
    assert ids(result) == ["2"]
    assert result.search_limited
    assert "ORIGIN_SEARCH_PARTIAL" in {w.code for w in result.warnings}


@pytest.mark.parametrize("fault", ["missing_id", "missing_name", "bad_coordinate"])
def test_invalid_record_does_not_drop_other_verified_candidates(fault):
    bad = poi(1)
    if fault == "missing_id":
        bad.pop("id")
    elif fault == "missing_name":
        bad["place_name"] = ""
    else:
        bad["x"] = "invalid"
    provider = provider_for(poi(2))
    provider.keyword.return_value = page([bad, poi(2)])
    result = search(provider)
    assert ids(result) == ["2"] and result.search_limited


@pytest.mark.parametrize(
    "body",
    [
        {"documents": [{"place_name": "좌표 없음"}], "meta": {"is_end": True}},
        {"documents": [], "meta": {"is_end": False}},
        {"documents": [], "meta": {"is_end": "true"}},
        page([poi(i) for i in range(1, 17)]),
        page([poi(1), poi(1, x="128")]),
    ],
)
def test_unusable_provider_response_is_an_error_not_no_results(body):
    provider = provider_for()
    provider.keyword.return_value = body
    with pytest.raises(ServiceError) as exc:
        search(provider)
    assert exc.value.code == "PROVIDER_ERROR"
    provider.region.assert_not_awaited()


def test_supported_area_is_derived_from_coordinates_and_conflicting_address_is_rejected():
    provider = provider_for(poi(1), poi(2))
    provider.region.side_effect = [region("인천광역시", "남동구"), region()]
    result = search(provider)
    assert ids(result) == ["2"] and result.search_limited
    assert "ORIGIN_SEARCH_PARTIAL" in {w.code for w in result.warnings}


def test_explicit_unsupported_origin_is_not_replaced_with_same_name_in_seoul():
    provider = provider_for(poi(1), poi(2, address="부산 연제구"))
    result = search(provider, hints=[hint("부산")])
    assert result.mode == "refine" and not result.candidates
    assert result.reason_code == "ORIGIN_OUT_OF_SERVICE_AREA"
    assert result.unsupported_candidates[0].provider_id == "2"
    assert clarification(result).choices == []


def test_better_unsupported_candidate_is_not_replaced_with_weaker_supported_name():
    provider = provider_for(poi(1, address="부산 연제구"), poi(2, "시청역 별관"))
    result = search(provider)
    assert result.mode == "refine" and result.reason_code == "ORIGIN_OUT_OF_SERVICE_AREA"


def test_empty_search_is_a_refinement_question_and_not_a_provider_failure():
    provider = provider_for()
    result = search(provider, "알수없는장소")
    assert result.mode == "refine" and result.reason_code == "ORIGIN_NOT_FOUND"
    assert clarification(result).choices == []
    assert provider.keyword.await_count == provider.address.await_count == 1


def test_conflicting_explicit_regions_ask_before_spending_search_calls():
    provider = provider_for()
    result = search(provider, hints=[hint("서울"), hint("인천")])
    assert result.reason_code == "ORIGIN_HINTS_CONFLICT"
    assert not provider.mock_calls


@pytest.mark.parametrize(
    "error,code",
    [
        (KakaoError(429, "한도", 60), "RATE_LIMITED"),
        (KakaoError(504, "시간 초과"), "PROVIDER_TIMEOUT"),
        (KakaoConfigurationError(502, "인증"), "SERVICE_UNAVAILABLE"),
        (KakaoError(502, "오류"), "PROVIDER_ERROR"),
    ],
)
def test_initial_provider_failure_preserves_error_without_search_retries(error, code):
    provider = provider_for()
    provider.keyword.side_effect = error
    with pytest.raises(ServiceError) as exc:
        search(provider)
    assert exc.value.code == code
    assert provider.keyword.await_count == 1
    provider.address.assert_not_awaited()
    provider.region.assert_not_awaited()


def test_failed_second_search_keeps_verified_first_results_with_warning():
    row = poi(1, category=None, category_name=None)
    provider = provider_for(row)
    provider.keyword.side_effect = [page([row]), KakaoError(504, "시간 초과")]
    result = search(provider)
    assert ids(result) == ["1"] and result.search_limited
    assert "PROVIDER_TIMEOUT" in {w.code for w in result.warnings}


def test_second_search_quota_stops_before_spending_coordinate_lookup_calls():
    row = poi(1, category=None, category_name=None)
    provider = provider_for(row)
    provider.keyword.side_effect = [page([row]), KakaoError(429, "한도", 60)]
    with pytest.raises(ServiceError) as exc:
        search(provider)
    assert exc.value.code == "RATE_LIMITED" and exc.value.retry_after_seconds == 60
    assert provider.keyword.await_count == 2
    provider.region.assert_not_awaited()


def test_all_region_lookups_failing_is_not_a_no_results_response():
    provider = provider_for(poi(1))
    provider.region.side_effect = KakaoError(504, "시간 초과")
    with pytest.raises(ServiceError) as exc:
        search(provider)
    assert exc.value.code == "PROVIDER_TIMEOUT"


def test_region_quota_stops_further_calls_and_preserves_already_verified_result():
    provider = provider_for(poi(1), poi(2), poi(3))
    provider.region.side_effect = [region(), KakaoError(429, "한도")]
    result = search(provider)
    assert ids(result) == ["1"] and provider.region.await_count == 2
    assert "RATE_LIMITED" in {w.code for w in result.warnings}


def test_maximum_display_and_lookup_budget_and_provider_pagination_are_explicit():
    rows = [poi(i, x=str(126.9 + i / 1000)) for i in range(1, 16)]
    provider = provider_for(*rows)
    provider.keyword.return_value = page(rows, more=True)
    result = search(provider)
    assert len(result.candidates) == 5 and result.search_limited
    assert provider.region.await_count == 5 and provider.keyword.await_count == 1
    assert result.mode == "choose"
    assert all(call.kwargs["page"] == 1 for call in provider.keyword.call_args_list)


def test_filtered_candidates_do_not_trigger_unbounded_region_lookup():
    rows = [poi(i, address="부산 연제구") for i in range(1, 16)]
    provider = provider_for(*rows)
    result = search(provider)
    assert not result.candidates and result.search_limited
    assert provider.region.await_count == 5


@pytest.mark.parametrize("name", ["", "   ", "x" * 101])
def test_invalid_search_input_never_reaches_provider(name):
    provider = provider_for()
    with pytest.raises(ServiceError) as exc:
        search(provider, name)
    assert exc.value.code == "VALIDATION_ERROR" and not provider.mock_calls


def test_shared_keyword_transport_only_adds_category_when_requested():
    async def run():
        calls = []
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: calls.append(request) or httpx.Response(200, json=page())
            )
        ) as http:
            client = KakaoLocalClient(http, "test-key")
            await client.keyword("시청역", page=1, size=15)
            await client.keyword("시청역", page=1, size=15, category="SW8")
        assert "category_group_code" not in calls[0].url.params
        assert calls[1].url.params["category_group_code"] == "SW8"
        assert calls[1].url.params["query"] == "시청역"

    asyncio.run(run())
