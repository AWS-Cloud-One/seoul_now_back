import pytest

from app.modules.data_pipeline.pipelines.snapshots import snapshot_outcome


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
