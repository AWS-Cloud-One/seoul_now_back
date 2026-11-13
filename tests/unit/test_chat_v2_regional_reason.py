from types import SimpleNamespace

from app.modules.chat_v2.regional_preparation import regional_reason


def test_regional_explanations_use_each_place_facts_with_readable_distance():
    def candidate(name, address, distance):
        return SimpleNamespace(
            place=SimpleNamespace(
                name=name, category_name="음식점 > 카페", road_address=address, address=address
            ),
            parking=SimpleNamespace(
                status="available", total_count=4, nearest_distance_meters=distance
            ),
        )

    first = regional_reason(candidate("첫 카페", "서울 마포구 희우정로 13", 17.8393), "망원동")
    second = regional_reason(candidate("둘째 카페", "서울 마포구 희우정로 21", 140), "망원동")
    assert first != second
    assert "첫 카페" in first and "희우정로 13" in first and "약 18m" in first
    assert "둘째 카페" in second and "약 140m" in second
    assert "17.8393" not in first
