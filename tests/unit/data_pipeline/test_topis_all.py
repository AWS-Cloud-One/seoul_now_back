import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.clients.seoul import CollectionError
from app.core.config import Settings
from app.modules.data_pipeline.collectors.topis import collect_topis
from app.modules.data_pipeline.pipelines.topis_rules import cycle_status, select_targets
from jobs import collect_topis as topis_job
from jobs import collect_topis_network as network_job


def test_all_scope_does_not_keep_area_or_twenty_link_limit():
    links, report = select_targets(["1000000300", "1220003800", "1000000400"], 1)
    assert links == ["1000000300", "1000000400", "1220003800"]
    assert report["selected_links"] == 3 and report["truncated"] is False


def test_scheduled_selection_is_bounded_and_uses_oldest_attempt_of_any_outcome():
    links, report = select_targets(
        ["a", "b", "c", "d", "d"],
        3,
        scheduled_batch=True,
        attempts={"a": "2026-11-02", "b": "2026-11-01"},
    )
    assert links == ["c", "d", "b"]
    assert report == {
        "scope": "scheduled_official_batch",
        "total_links": 4,
        "selected_links": 3,
        "truncated": True,
    }


@pytest.mark.parametrize("links", [None, []])
def test_missing_official_roads_prevent_collection(links):
    with pytest.raises(ValueError, match="missing_official_geometry"):
        select_targets(links, 20)


@pytest.mark.parametrize(
    "counts, outcomes, stopped, expected",
    [
        ({"success": 3}, {"success": 3}, False, "success"),
        ({"provider_no_observation": 2}, {"failed": 2}, False, "partial"),
        ({"missing_road_geometry": 1}, {"partial": 1}, False, "partial"),
        ({"provider_no_observation": 1, "provider_http_error": 1}, {"failed": 2}, False, "failed"),
        ({"normalization_failed": 1}, {"failed": 1}, False, "failed"),
        (
            {"success": 1, "provider_network_error": 1},
            {"success": 1, "failed": 1},
            False,
            "partial",
        ),
        (
            {"some_values": 1, "provider_server_error": 1},
            {"partial": 1, "failed": 1},
            False,
            "partial",
        ),
        ({"success": 1, "normalization_failed": 1}, {"success": 1, "failed": 1}, False, "partial"),
        ({}, {}, True, "paused_provider_limit"),
    ],
)
def test_cycle_status_preserves_outcome_priority(counts, outcomes, stopped, expected):
    assert cycle_status(counts, outcomes, stopped) == expected


@pytest.mark.parametrize("job", [topis_job, network_job])
@pytest.mark.parametrize(
    "status, code",
    [
        ("success", 0),
        ("partial", 2),
        ("failed", 1),
        ("paused_provider_limit", 4),
    ],
)
def test_jobs_pass_resources_return_exit_codes_and_dispose(monkeypatch, capsys, job, status, code):
    engine = SimpleNamespace(dispose=AsyncMock())
    dedicated = "dedicated" if job is topis_job else ""
    settings = Settings(_env_file=None, seoul_topis_api_key=dedicated, seoul_api_key="fallback")
    monkeypatch.setattr(job, "Settings", lambda: settings)
    monkeypatch.setattr(job, "create_engine", lambda settings: engine)
    clients = []

    async def fetch(client, key, link):
        assert key == (dedicated or "fallback") and link == "1000000000"
        clients.append(client)
        assert not client.is_closed

    async def pipeline(resource, fetch_link, *args):
        assert resource is engine
        await fetch_link("1000000000")
        return {"status": status}

    monkeypatch.setattr(job, "collect_topis", fetch)
    monkeypatch.setattr(job, "run_topis" if job is topis_job else "run_topis_network", pipeline)
    call = (
        topis_job.execute(["1000000000"]) if job is topis_job else network_job.execute(20, 0, True)
    )
    assert asyncio.run(call) == code
    assert clients[0].is_closed
    engine.dispose.assert_awaited_once()
    assert [json.loads(line) for line in capsys.readouterr().out.splitlines()] == [
        {"status": status}
    ]


@pytest.mark.parametrize(
    "status, delay", [("success", 300), ("failed", 300), ("paused_provider_limit", 3600)]
)
def test_explicit_repeat_preserves_limit_backoff(monkeypatch, status, delay):
    engine = SimpleNamespace(dispose=AsyncMock())
    monkeypatch.setattr(network_job, "Settings", lambda: Settings(_env_file=None))
    monkeypatch.setattr(network_job, "create_engine", lambda settings: engine)
    pipeline = AsyncMock(return_value={"status": status})
    monkeypatch.setattr(network_job, "run_topis_network", pipeline)
    delays = []

    async def sleep(seconds):
        delays.append(seconds)
        raise asyncio.CancelledError

    monkeypatch.setattr(network_job.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(network_job.execute(200, 300, True))
    assert delays == [delay]
    pipeline.assert_awaited_once()
    engine.dispose.assert_awaited_once()


def test_quota_and_auth_errors_are_classified_without_key_leak():
    async def run():
        for code, expected in [
            ("ERROR-337", "provider_quota_exceeded"),
            ("INFO-100", "provider_auth_error"),
            ("INFO-200", "provider_no_observation"),
        ]:

            async def handler(request, code=code):
                return httpx.Response(200, text=f"<RESULT><CODE>{code}</CODE></RESULT>")

            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                with pytest.raises(CollectionError) as error:
                    await collect_topis(client, "secret-example-key", "1000000300")
                assert str(error.value) == expected
                assert "secret-example-key" not in str(error.value)

    asyncio.run(run())


@pytest.mark.parametrize(
    "status,body,expected",
    [
        (503, "unavailable", "provider_server_error"),
        (429, "limited", "provider_quota_exceeded"),
        (401, "unauthorized", "provider_auth_error"),
        (403, "forbidden", "provider_auth_error"),
        (404, "not found", "provider_http_error"),
        (200, "invalid xml", "provider_invalid_xml"),
    ],
)
def test_http_errors_distinguish_retryable_server_failure_from_permanent_errors(
    status, body, expected
):
    async def run():
        async def handler(request):
            return httpx.Response(status, text=body)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(CollectionError) as error:
                await collect_topis(client, "secret-example-key", "1000000300")
            assert str(error.value) == expected

    asyncio.run(run())
