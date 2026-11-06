import asyncio
from unittest.mock import AsyncMock

from app.modules.ai.explanation import explain
from app.schemas.recommendation import Candidate, Place


def test_unverified_claim_selection_falls_back_to_known_eta():
    item = Candidate(place=Place(id="1", name="파스타", x=127, y=37.5), duration_seconds=1260)
    model = AsyncMock()
    model.complete.return_value = {"items": [{"place_id": "1", "facts": ["parking"]}]}
    results, source = asyncio.run(explain(model, [item]))
    assert source == "template"
    assert "21분" in results[0].explanation
    assert "확인 불가" in results[0].explanation
    assert "잔여" not in results[0].explanation


def test_model_cannot_change_order():
    items = [
        Candidate(place=Place(id=str(i), name="파스타", x=127, y=37.5), duration_seconds=600)
        for i in (1, 2)
    ]
    model = AsyncMock()
    model.complete.return_value = {
        "items": [{"place_id": "2", "facts": ["eta"]}, {"place_id": "1", "facts": ["eta"]}]
    }
    results, source = asyncio.run(explain(model, items))
    assert [c.place.id for c in results] == ["1", "2"]
    assert source == "template"
