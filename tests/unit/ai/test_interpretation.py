import pytest

from app.modules.ai.interpretation import merge_conditions
from app.schemas.recommendation import Conditions, Interpretation


def test_keyword_patch_preserves_origin_and_limit():
    current = Conditions(query="고기", origin_name="시청역", x=126.978, y=37.5665, max_minutes=45)
    result = merge_conditions(
        current, Interpretation(request_type="update_conditions", changes={"query": "파스타"})
    )
    assert result.query == "파스타"
    assert (result.x, result.y, result.max_minutes) == (
        126.978,
        37.5665,
        45,
    )


def test_new_defaults_to_thirty_and_origin_change_clears_coordinates():
    assert (
        merge_conditions(
            None, Interpretation(request_type="new_recommendation", changes={"query": "파스타"})
        ).max_minutes
        == 30
    )
    current = Conditions(query="파스타", x=126.978, y=37.5665)
    result = merge_conditions(
        current, Interpretation(request_type="update_conditions", changes={"origin_name": "강남역"})
    )
    assert result.x is None and result.y is None


def test_more_cannot_change_conditions_or_invent_history():
    with pytest.raises(ValueError):
        merge_conditions(None, Interpretation(request_type="recommend_more", changes={}))
    with pytest.raises(ValueError):
        merge_conditions(
            Conditions(query="고기"),
            Interpretation(request_type="recommend_more", changes={"query": "파스타"}),
        )
