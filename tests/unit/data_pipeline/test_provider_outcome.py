import pytest

from app.repositories.provider import snapshot_outcome


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"items": [1]}, "success"),
        ({"datasets": {"a": {"complete": True}}}, "success"),
        ({"datasets": {"a": {"complete": True}, "b": {"complete": False}}}, "partial"),
    ],
)
def test_snapshot_completeness_controls_collection_status(payload, expected):
    assert snapshot_outcome(payload) == expected
