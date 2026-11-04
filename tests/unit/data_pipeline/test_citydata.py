import asyncio
from copy import deepcopy
from datetime import UTC, datetime
from unittest.mock import patch
from uuid import UUID

import httpx
import pytest

from app.clients.seoul import CollectionError
from app.modules.data_pipeline.collectors.citydata import collect_citydata, resolve_citydata_area
from app.modules.data_pipeline.normalizers.citydata import CityDataError, population_row
from app.modules.data_pipeline.normalizers.storage_fields import population
from app.modules.data_pipeline.pipelines import snapshots


def city():
    """Small fixture following the OA-21285 JSON sample, without unselected content."""
    return {
        "AREA_NM": "광화문·덕수궁",
        "AREA_CD": "POI009",
        "LIVE_PPLTN_STTS": [
            {
                "AREA_NM": "광화문·덕수궁",
                "AREA_CD": "POI009",
                "AREA_PPLTN_MIN": "0",
                "AREA_PPLTN_MAX": "10",
                "AREA_CONGEST_LVL": "여유",
                "REPLACE_YN": "N",
                "PPLTN_TIME": "2026-11-04 06:30",
            }
        ],
        "PRK_STTS": [],
        "UNSELECTED_SECTION": {"unknown_shape": [1, 2]},
    }


def fetch(body):
    async def run():
        def handler(request):
            assert request.url.path.endswith("/광화문·덕수궁")
            return httpx.Response(200, json=body)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await collect_citydata(
                client, "private-key", "광화문·덕수궁", expected_code="POI009"
            )

    return asyncio.run(run())


@pytest.mark.parametrize(
    "result",
    [
        {"CODE": "INFO-000"},
        {"RESULT.CODE": "INFO-000"},
        {"CODE": "INFO-000", "RESULT.CODE": "INFO-000"},
    ],
)
def test_real_result_key_forms_preserve_unselected_sections(result):
    data = city()
    document = fetch({"RESULT": result, "CITYDATA": data})
    assert document.body == {"RESULT": result, "CITYDATA": data}
    assert document.received_at.tzinfo is not None


@pytest.mark.parametrize(
    "result,error",
    [
        ({"CODE": "ERROR-200"}, "provider_result_error"),
        ({"RESULT.CODE": "ERROR-200"}, "provider_result_error"),
        ({"RESULT.CODE": "INFO-200"}, "provider_no_observation"),
        ({"CODE": "INFO-100"}, "provider_auth_error"),
        ({"CODE": "INFO-101"}, "provider_auth_error"),
        ({"CODE": "ERROR-337"}, "provider_quota_exceeded"),
        ({"CODE": "INFO-000", "RESULT.CODE": "ERROR-200"}, "provider_invalid_response"),
        ({}, "provider_invalid_response"),
        (None, "provider_invalid_response"),
        ({"RESULT.CODE": []}, "provider_invalid_response"),
    ],
)
def test_result_failures_are_not_hidden_by_valid_citydata(result, error):
    with pytest.raises(CollectionError, match=f"^{error}$"):
        fetch({"RESULT": result, "CITYDATA": city()})


@pytest.mark.parametrize(
    "status,error",
    [(401, "provider_auth_error"), (403, "provider_auth_error"), (429, "provider_quota_exceeded")],
)
def test_common_http_failures_are_identified_without_immediate_retries(status, error):
    calls = []

    async def run():
        def handler(request):
            calls.append(status)
            return httpx.Response(status)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(CollectionError, match=f"^{error}$"):
                await collect_citydata(client, "test", "광화문·덕수궁", expected_code="POI009")

    asyncio.run(run())
    assert calls == [status]


@pytest.mark.parametrize(
    "updates,error",
    [
        ({"AREA_CD": "POI010"}, "area_identity_mismatch"),
        ({"AREA_NM": "서울역"}, "area_identity_mismatch"),
        ({"AREA_CD": ""}, "missing_area_identity"),
        ({"AREA_CD": 9}, "missing_area_identity"),
    ],
)
def test_request_code_and_name_must_both_match(updates, error):
    with pytest.raises(CollectionError, match=f"^{error}$"):
        fetch({"RESULT": {"CODE": "INFO-000"}, "CITYDATA": {**city(), **updates}})


def test_cli_resolves_official_code_or_name_and_rejects_unrecognized_area():
    targets = {"POI009": "광화문·덕수궁", "POI010": "다른 장소"}
    for value in (" POI009 ", "광화문·덕수궁"):
        assert resolve_citydata_area(value, targets) == ("POI009", "광화문·덕수궁")
    with pytest.raises(CollectionError, match="unknown_or_ambiguous_area"):
        resolve_citydata_area("unknown", targets)
    with pytest.raises(CollectionError, match="unknown_or_ambiguous_area"):
        resolve_citydata_area("same", {"POI001": "same", "POI002": "same"})


@pytest.mark.parametrize(
    "section,error",
    [
        (None, "invalid_population_section"),
        ([], "empty_population_section"),
        (["bad"], "invalid_population_section"),
        ([{}], "invalid_population_section"),
    ],
)
def test_missing_empty_and_invalid_population_are_distinct(section, error):
    with pytest.raises(CityDataError, match=f"^{error}$"):
        population_row({**city(), "LIVE_PPLTN_STTS": section})
    without = city()
    without.pop("LIVE_PPLTN_STTS")
    with pytest.raises(CityDataError, match="^missing_population_section$"):
        population_row(without)


def test_population_identity_and_conflict_are_rejected_before_any_write():
    for change in ({"AREA_CD": "POI010"}, {"AREA_NM": "서울역"}, {"AREA_CD": None}):
        data = city()
        data["LIVE_PPLTN_STTS"][0].update(change)
        with patch.object(snapshots, "NormalizedWriter") as writer:
            assert (
                snapshots.normalize_snapshot(None, "seoul_citydata", data, UUID(int=1)).error_code
                == "population_area_mismatch"
            )
            writer.return_value.upsert.assert_not_called()

    data = city()
    data["LIVE_PPLTN_STTS"].append({**data["LIVE_PPLTN_STTS"][0], "AREA_PPLTN_MAX": "20"})
    for rows in (data["LIVE_PPLTN_STTS"], list(reversed(data["LIVE_PPLTN_STTS"]))):
        with pytest.raises(CityDataError, match="^conflicting_population_rows$"):
            population_row({**data, "LIVE_PPLTN_STTS": rows})


def test_equal_population_observations_ignore_unselected_forecast_differences():
    data = city()
    original = deepcopy(data)
    data["LIVE_PPLTN_STTS"].append({**data["LIVE_PPLTN_STTS"][0], "FCST_PPLTN": ["unused"]})
    assert population(data) == population(original)
    assert population(data)["min_people"] == 0


@pytest.mark.parametrize(
    "stamp,error",
    [
        ("2099-01-01 00:00", "future_population_time"),
        (123, "invalid_population_time"),
        ("2026-11-04", "invalid_population_time"),
    ],
)
def test_bad_observation_time_does_not_become_a_new_unknown_time_write(stamp, error):
    data = city()
    data["LIVE_PPLTN_STTS"][0]["PPLTN_TIME"] = stamp
    with patch.object(snapshots, "NormalizedWriter") as writer:
        assert (
            snapshots.normalize_snapshot(
                None,
                "seoul_citydata",
                data,
                UUID(int=1),
                received_at=datetime(2026, 11, 4, tzinfo=UTC),
            ).error_code
            == error
        )
        writer.return_value.upsert.assert_not_called()
