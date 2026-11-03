import json
from unittest.mock import AsyncMock

import pytest

from jobs import collect_citydata_all, collect_supplemental


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


def test_probe_uses_local_raw_directory_and_reports_stored_outcome(monkeypatch, tmp_path, capsys):
    engine = AsyncMock()
    monkeypatch.setattr(collect_supplemental, "create_engine", lambda _: engine)
    monkeypatch.setattr(collect_supplemental, "RAW_DATA", tmp_path / "raw")
    monkeypatch.setattr(collect_supplemental, "REFERENCE_DATA", tmp_path / "reference")
    pipeline = AsyncMock(return_value={"status": "partial", "records": 3})
    monkeypatch.setattr(collect_supplemental, "run_supplemental", pipeline)
    monkeypatch.setattr("sys.argv", ["collect_supplemental", "cctv", "--reuse-probe"])
    with pytest.raises(SystemExit) as result:
        collect_supplemental.main()
    assert result.value.code == 2
    kwargs = pipeline.call_args.kwargs
    assert kwargs["probe_path"] == tmp_path / "raw/its_probe.json"
    assert kwargs["sensor_path"] == tmp_path / "reference/sdot_locations_20251113.xlsx"
    assert json.loads(capsys.readouterr().out) == {"status": "partial", "records": 3}
    engine.dispose.assert_awaited_once()
