import asyncio
import json
import sys
from unittest.mock import AsyncMock

import pytest

from app.core.config import Settings
from app.modules.data_pipeline.pipelines.topis_rules import cycle_status
from jobs import scheduler
from jobs.runtime import finish


def test_enabled_providers_exclude_its_and_sgis_requires_both_credentials():
    assert set(scheduler.TASKS) == {
        "citydata",
        "parking",
        "municipal",
        "areas",
        "sdot",
        "national-parking",
        "topis",
    }
    assert scheduler.TASKS["areas"].all_keys
    assert scheduler.TASKS["areas"].key_names == ("sgis_consumer_key", "sgis_consumer_secret")


def test_publication_recovery_retries_without_calling_provider(monkeypatch):
    publish = AsyncMock(
        side_effect=[
            {"publication": {"status": "failed"}},
            {"publication": {"status": "success"}},
        ]
    )
    command = AsyncMock()
    monkeypatch.setattr(scheduler, "maintain_cycles", AsyncMock())
    monkeypatch.setattr(scheduler, "publish_result", publish)
    monkeypatch.setattr(scheduler, "run_command", command)
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    result = asyncio.run(scheduler.recover_publication(None, "topis"))
    assert result["status"] == "success" and publish.await_count == 2
    assert publish.await_args.kwargs == {"geometry": (), "observations": ("roads",)}
    command.assert_not_awaited()


def test_command_timeout_kills_and_reaps_real_child(monkeypatch):
    original = asyncio.create_subprocess_exec
    children = []

    async def subprocess(*args, **kwargs):
        child = await original(sys.executable, "-c", "import time; time.sleep(30)", **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", subprocess)

    async def run():
        task = scheduler.Task("unused", 600, (), timeout=0.05)
        with pytest.raises(TimeoutError):
            await scheduler.run_command(task)
        assert len(children) == 1 and children[0].returncode is not None
        assert children[0].returncode != 0

    asyncio.run(run())


def test_missing_sgis_secret_is_skipped_without_running_collection(monkeypatch):
    monkeypatch.setattr(
        scheduler,
        "Settings",
        lambda: Settings(_env_file=None, sgis_consumer_key="test", sgis_consumer_secret=""),
    )
    assert asyncio.run(scheduler.execute(["areas"], once=True)) == 3


def test_topis_rotates_uncollected_and_oldest_links():
    from app.modules.data_pipeline.pipelines.topis_rules import scheduled_links

    assert scheduled_links(["a", "b", "c", "d"], {"a": "2026-01-02", "b": "2026-01-01"}, 3) == [
        "c",
        "d",
        "b",
    ]


def test_worker_retries_failure_and_uses_remaining_interval(monkeypatch):
    calls, delays = [], []
    elapsed = 0

    async def cycle(*args):
        nonlocal elapsed
        calls.append(args[1])
        if len(calls) == 1:
            elapsed += 5
            raise RuntimeError("database unavailable")
        elapsed += 20
        return scheduler.TaskResult("success", 0)

    async def sleep(delay):
        nonlocal elapsed
        delays.append(delay)
        elapsed += delay
        if len(delays) == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(scheduler, "cycle", cycle)

    async def run():
        try:
            await scheduler.worker(
                None,
                "citydata",
                scheduler.TASKS["citydata"],
                sleep=sleep,
                clock=lambda: elapsed,
            )
        except asyncio.CancelledError:
            pass

    asyncio.run(run())
    assert calls == ["citydata", "citydata"]
    assert delays == [300, 580]


@pytest.mark.parametrize(
    "name,status,expected",
    [
        ("areas", "failed", 300),
        ("parking", "failed", 300),
        ("sdot", "failed", 600),
        ("national-parking", "failed", 3600),
        ("topis", "failed", 3600),
        ("topis", "paused_provider_limit", 3600),
        ("citydata", "paused_provider_limit", 3600),
        ("sdot", "waiting", 60),
    ],
)
def test_failure_and_waiting_delays_start_after_attempt(monkeypatch, name, status, expected):
    elapsed = 0
    delays = []

    async def cycle(*args):
        nonlocal elapsed
        elapsed += 1800
        return scheduler.TaskResult(status, 4 if status == "paused_provider_limit" else 1)

    async def sleep(delay):
        delays.append(delay)
        raise asyncio.CancelledError

    monkeypatch.setattr(scheduler, "cycle", cycle)

    async def run():
        with pytest.raises(asyncio.CancelledError):
            await scheduler.worker(
                None, name, scheduler.TASKS[name], sleep=sleep, clock=lambda: elapsed
            )

    asyncio.run(run())
    assert delays == [expected]


@pytest.mark.parametrize("duration,expected", [(20, 580), (650, 1)])
def test_topis_mixed_outcomes_keep_full_network_ten_minute_cadence(monkeypatch, duration, expected):
    elapsed = 0
    delays = []

    async def cycle(*args):
        nonlocal elapsed
        elapsed += duration
        status = cycle_status(
            {"success": 99, "provider_network_error": 1}, {"success": 99, "failed": 1}, False
        )
        code = finish({"status": status})
        assert status == "partial" and code == 2
        return scheduler.TaskResult(status, code)

    async def sleep(delay):
        delays.append(delay)
        raise asyncio.CancelledError

    monkeypatch.setattr(scheduler, "cycle", cycle)
    assert scheduler.TASKS["topis"].args == ("--all",)

    async def run():
        with pytest.raises(asyncio.CancelledError):
            await scheduler.worker(
                None, "topis", scheduler.TASKS["topis"], sleep=sleep, clock=lambda: elapsed
            )

    asyncio.run(run())
    assert delays == [expected]


@pytest.mark.parametrize(
    "outcomes,expected",
    [
        ([scheduler.TaskResult("success", 0)], 0),
        ([scheduler.TaskResult("partial", 2)], 2),
        ([scheduler.TaskResult("success", 0), scheduler.TaskResult("skipped", 3)], 2),
        ([scheduler.TaskResult("skipped", 3)], 3),
        ([scheduler.TaskResult("waiting", 1)], 1),
        ([scheduler.TaskResult("success", 0), scheduler.TaskResult("failed", 127)], 1),
        ([scheduler.TaskResult("paused_provider_limit", 4), scheduler.TaskResult("waiting", 1)], 4),
    ],
)
def test_once_aggregate_preserves_incomplete_and_failed_outcomes(outcomes, expected):
    assert scheduler.aggregate(outcomes).exit_code == expected


def configure_execute(monkeypatch, **keys):
    monkeypatch.setattr(scheduler, "Settings", lambda: Settings(_env_file=None, **keys))
    engine = AsyncMock()
    monkeypatch.setattr(scheduler, "create_engine", lambda settings: engine)
    return engine


@pytest.mark.parametrize("child_status,child_code", [("success", 0), ("partial", 2)])
def test_once_does_not_hide_missing_selected_keys(monkeypatch, capsys, child_status, child_code):
    engine = configure_execute(monkeypatch, seoul_citydata_api_key="test")
    cycle = AsyncMock(return_value=scheduler.TaskResult(child_status, child_code))
    monkeypatch.setattr(scheduler, "cycle", cycle)
    assert asyncio.run(scheduler.execute(["citydata", "areas"], once=True)) == 2
    result = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert result["results"]["areas"] == {
        "status": "skipped",
        "exit_code": 3,
        "error_code": "missing_api_key",
    }
    assert result["results"]["citydata"]["status"] == child_status
    cycle.assert_awaited_once()
    engine.dispose.assert_awaited_once()


@pytest.mark.parametrize("area_status", ["success", "partial", "failed"])
def test_selected_dependencies_finish_their_first_cycle_before_consumers(monkeypatch, area_status):
    configure_execute(
        monkeypatch,
        seoul_api_key="test",
        sgis_consumer_key="test",
        sgis_consumer_secret="test",
        seoul_sdot_api_key="test",
        seoul_citydata_api_key="test",
    )
    completed = []
    independent_started = []

    async def cycle(engine, name, task):
        if name == "citydata":
            independent_started.append(not completed)
        if name == "sdot":
            assert "areas" in completed
        if name == "municipal":
            assert "parking" in completed
        await asyncio.sleep(0)
        completed.append(name)
        return (
            scheduler.TaskResult(
                area_status, {"success": 0, "partial": 2, "failed": 1}[area_status]
            )
            if name == "areas"
            else scheduler.TaskResult("success", 0)
        )

    monkeypatch.setattr(scheduler, "cycle", cycle)
    code = asyncio.run(
        scheduler.execute(["sdot", "municipal", "areas", "parking", "citydata"], once=True)
    )
    assert code == {"success": 0, "partial": 2, "failed": 1}[area_status]
    assert independent_started == [True]
    assert set(completed) == {"sdot", "municipal", "areas", "parking", "citydata"}


def test_unselected_prerequisite_task_is_not_automatically_collected(monkeypatch):
    configure_execute(monkeypatch, seoul_sdot_api_key="test")
    cycle = AsyncMock(return_value=scheduler.TaskResult("success", 0))
    monkeypatch.setattr(scheduler, "cycle", cycle)
    assert asyncio.run(scheduler.execute(["sdot"], once=True)) == 0
    assert cycle.await_args.args[1] == "sdot"
    cycle.assert_awaited_once()


def test_timeout_kills_child_and_waits_for_exit(monkeypatch):
    class Process:
        returncode = None
        killed = False

        async def wait(self):
            if not self.killed:
                await asyncio.Event().wait()
            self.returncode = -1
            return -1

        def kill(self):
            self.killed = True

    process = Process()

    async def spawn(*args, **kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)

    async def run():
        try:
            await scheduler.run_command(scheduler.Task("test", 300, (), timeout=0.01))
        except TimeoutError:
            pass

    asyncio.run(run())
    assert process.killed
    assert process.returncode == -1
