import json
from unittest.mock import AsyncMock

import pytest

from jobs import collect_citydata_all, collect_supplemental
from jobs.runtime import finish


@pytest.mark.parametrize("publication", ["failed", "deferred"])
@pytest.mark.parametrize(
    "status, code", [("success", 2), ("failed", 1), ("paused_provider_limit", 4)]
)
def test_publication_outcome_controls_exit_without_relabeling_collection(
    capsys, publication, status, code
):
    result = {"status": status, "publication": {"status": publication}}
    assert finish(result) == code
    assert json.loads(capsys.readouterr().out) == result


@pytest.mark.parametrize(
    "error, code", [(RuntimeError("private provider URL"), 1), (KeyboardInterrupt(), 130)]
)
def test_main_exits_cleanly_without_exposing_exception_details(monkeypatch, capsys, error, code):
    monkeypatch.setattr("sys.argv", ["collect_citydata_all"])
    operation = AsyncMock(side_effect=error)
    monkeypatch.setattr(collect_citydata_all, "execute", operation)
    with pytest.raises(SystemExit) as result:
        collect_citydata_all.main()
    assert result.value.code == code
    operation.assert_awaited_once_with(0)
    captured = capsys.readouterr()
    assert "private" not in captured.out + captured.err
    if code == 1:
        assert json.loads(captured.out) == {
            "status": "failed",
            "error_code": "configuration_or_database_error",
        }


def test_supplemental_reports_outcome_without_file_paths(monkeypatch, capsys):
    engine = AsyncMock()
    monkeypatch.setattr(collect_supplemental, "create_engine", lambda _: engine)
    pipeline = AsyncMock(return_value={"status": "partial", "records": 3})
    monkeypatch.setattr(collect_supplemental, "run_supplemental", pipeline)
    monkeypatch.setattr("sys.argv", ["collect_supplemental", "sdot"])
    with pytest.raises(SystemExit) as result:
        collect_supplemental.main()
    assert result.value.code == 2
    kwargs = pipeline.call_args.kwargs
    assert kwargs == {}
    assert json.loads(capsys.readouterr().out) == {"status": "partial", "records": 3}
    engine.dispose.assert_awaited_once()
