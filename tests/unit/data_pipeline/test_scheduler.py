import asyncio

from app.core.config import Settings
from jobs import scheduler


def test_all_providers_are_default_tasks_and_sgis_requires_both_credentials():
    assert set(scheduler.TASKS) == {
        "citydata",
        "parking",
        "municipal",
        "areas",
        "sdot",
        "national-parking",
        "cctv",
        "topis",
    }
    assert scheduler.TASKS["areas"].all_keys
    assert scheduler.TASKS["areas"].key_names == ("sgis_consumer_key", "sgis_consumer_secret")


def test_missing_sgis_secret_is_skipped_without_running_collection(monkeypatch):
    monkeypatch.setattr(
        scheduler,
        "Settings",
        lambda: Settings(_env_file=None, sgis_consumer_key="test", sgis_consumer_secret=""),
    )
    try:
        asyncio.run(scheduler.main(["areas"], once=True))
    except SystemExit as exc:
        assert str(exc) == "No configured collection tasks"
    else:
        raise AssertionError("SGIS must require both credentials")


def test_topis_rotates_uncollected_and_oldest_links():
    from jobs.collect_topis_network import scheduled_links

    assert scheduled_links(["a", "b", "c", "d"], {"a": "2026-01-02", "b": "2026-01-01"}, 3) == [
        "c",
        "d",
        "b",
    ]


def test_worker_retries_failure_and_uses_remaining_interval(monkeypatch):
    calls, delays = [], []

    async def cycle(*args):
        calls.append(args[1])
        if len(calls) == 1:
            raise RuntimeError("database unavailable")
        return True

    async def sleep(delay):
        delays.append(delay)
        if len(delays) == 2:
            raise asyncio.CancelledError

    times = iter([0, 5, 300, 320])
    monkeypatch.setattr(scheduler, "cycle", cycle)

    async def run():
        try:
            await scheduler.worker(
                None,
                "citydata",
                scheduler.TASKS["citydata"],
                sleep=sleep,
                clock=lambda: next(times),
            )
        except asyncio.CancelledError:
            pass

    asyncio.run(run())
    assert calls == ["citydata", "citydata"]
    assert delays == [295, 280]


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
