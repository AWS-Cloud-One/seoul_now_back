import pytest

from app.modules.recommendation.state import StateCodec
from app.schemas.recommendation import Conditions, SearchState


def test_state_tampering_and_expiry_rejected():
    codec = StateCodec("secret-for-unit-tests")
    state = SearchState(conditions=Conditions(query="파스타"), expires_at=100)
    token = codec.encode(state)
    assert codec.decode(token, now=99).conditions.query == "파스타"
    with pytest.raises(ValueError):
        codec.decode(token, now=100)
    with pytest.raises(ValueError):
        codec.decode("x" + token[1:], now=99)
