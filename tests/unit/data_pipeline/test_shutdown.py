import asyncio
import json
import os
import select
import signal
import subprocess
import sys
import time
from unittest.mock import AsyncMock, Mock

import pytest

from jobs import scheduler, shutdown


def environment():
    return {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith(("APP_", "AWS_", "PG"))
    }


def wait_ready(process):
    assert select.select([process.stdout], [], [], 5)[0], "job never became ready"
    assert process.stdout.readline().strip() == "ready"


def test_concurrent_process_logs_remain_separate_json_lines():
    child = """
import os
from jobs.runtime import report
for i in range(1000):
    report({"status": "종료", "pid": os.getpid(), "sequence": i})
"""
    parent = f"""
import subprocess, sys
children = [subprocess.Popen([sys.executable, '-u', '-c', {child!r}]) for _ in range(4)]
assert all(child.wait() == 0 for child in children)
"""
    result = subprocess.run(
        [sys.executable, "-u", "-c", parent],
        env=environment(),
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    records = [json.loads(line) for line in result.stdout.splitlines()]
    assert len(records) == 4000
    assert len({record["pid"] for record in records}) == 4
    assert all(record["status"] == "종료" for record in records)


@pytest.mark.parametrize(
    "signum,repeat", [(signal.SIGTERM, False), (signal.SIGINT, False), (signal.SIGTERM, True)]
)
def test_real_signals_finish_cleanup_and_preserve_interruption_exit(signum, repeat):
    script = """
import asyncio
from jobs.runtime import run_cli
async def operation():
    print("ready", flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        print("cleanup-start", flush=True)
        await asyncio.sleep(0.3)
        print("cleanup-done", flush=True)
run_cli(operation)
"""
    with subprocess.Popen(
        [sys.executable, "-u", "-c", script],
        env=environment(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as process:
        try:
            wait_ready(process)
            process.send_signal(signum)
            if repeat:
                time.sleep(0.1)
                process.send_signal(signal.SIGTERM)
                process.send_signal(signal.SIGINT)
            out, err = process.communicate(timeout=5)
            assert process.returncode == 128 + signum
            assert "cleanup-done" in out and not err
            assert out.count('"shutdown_requested"') == 1
            assert out.count('"shutdown_completed"') == 1
        finally:
            if process.poll() is None:
                process.kill()


def test_shutdown_deadline_also_bounds_executor_thread_drain():
    script = """
import asyncio, time
from jobs import shutdown
from jobs.runtime import run_cli
shutdown.SHUTDOWN_SECONDS = 0.4
def blocking():
    print("ready", flush=True)
    time.sleep(30)
async def operation():
    await asyncio.to_thread(blocking)
run_cli(operation)
"""
    with subprocess.Popen(
        [sys.executable, "-u", "-c", script],
        env=environment(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as process:
        try:
            wait_ready(process)
            started = time.monotonic()
            process.terminate()
            out, err = process.communicate(timeout=3)
            assert process.returncode == 143
            assert time.monotonic() - started < 2
            assert "shutdown_completed" not in out
            assert json.loads(err)["error_code"] == "shutdown_deadline_exceeded"
        finally:
            if process.poll() is None:
                process.kill()


@pytest.mark.parametrize("cancel_during_spawn", [False, True])
def test_cancelled_spawn_or_wait_reaps_real_child_despite_repeated_cancellation(
    monkeypatch, tmp_path, cancel_during_spawn
):
    ready = tmp_path / "ready"
    code = (
        "import signal,time,pathlib; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"pathlib.Path({str(ready)!r}).touch(); time.sleep(30)"
    )
    original = asyncio.create_subprocess_exec
    children = []
    monkeypatch.setattr(shutdown, "CHILD_TERM_SECONDS", 0.1)

    async def run():
        spawned = asyncio.Event()
        release = asyncio.Event()

        async def spawn(*args, **kwargs):
            child = await original(sys.executable, "-u", "-c", code, **kwargs)
            children.append(child)
            for _ in range(300):
                if ready.exists():
                    break
                await asyncio.sleep(0.01)
            else:
                raise AssertionError("child did not initialize its signal handler")
            spawned.set()
            if cancel_during_spawn:
                await release.wait()
            return child

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        running = asyncio.create_task(scheduler.run_command(scheduler.Task("test", 1, ())))
        await asyncio.wait_for(spawned.wait(), 3)
        running.cancel()
        release.set()
        await asyncio.sleep(0.03)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(running, 3)
        assert children[0].returncode == -signal.SIGKILL
        with pytest.raises(ProcessLookupError):
            os.kill(children[0].pid, 0)

    asyncio.run(run())


def test_cancellation_defers_post_collection_recovery(monkeypatch, capsys):
    recover = AsyncMock()
    monkeypatch.setattr(scheduler, "recover_publication", recover)

    async def run():
        entered = asyncio.Event()
        cleaned = []

        async def command(task):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0.01)
                cleaned.append(True)

        monkeypatch.setattr(scheduler, "run_command", command)
        running = asyncio.create_task(scheduler.run_cycle(None, "citydata", None))
        await entered.wait()
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert cleaned == [True]

    asyncio.run(run())
    recover.assert_awaited_once()
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["status"] == "publication_deferred"


def test_cancellation_during_startup_recovery_never_starts_child(monkeypatch):
    command = AsyncMock()
    monkeypatch.setattr(scheduler, "run_command", command)

    async def run():
        entered = asyncio.Event()

        async def recover(*args):
            entered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(scheduler, "recover_publication", recover)
        running = asyncio.create_task(scheduler.run_cycle(None, "citydata", None))
        await entered.wait()
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running

    asyncio.run(run())
    command.assert_not_awaited()


def test_normal_timeout_still_runs_publication_recovery(monkeypatch):
    recover = AsyncMock()
    monkeypatch.setattr(scheduler, "recover_publication", recover)
    monkeypatch.setattr(scheduler, "run_command", AsyncMock(side_effect=TimeoutError))
    with pytest.raises(TimeoutError):
        asyncio.run(scheduler.run_cycle(None, "citydata", None))
    assert recover.await_count == 2


def test_lost_lease_cancels_and_drains_work_before_returning(monkeypatch):
    async def run():
        started, cleaned = asyncio.Event(), asyncio.Event()

        async def work(*args):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0.01)
                cleaned.set()

        async def monitor(*args):
            await started.wait()
            raise scheduler.LeaseLostError

        monkeypatch.setattr(scheduler, "run_cycle", work)
        monkeypatch.setattr(scheduler, "monitor_lease", monitor)
        with pytest.raises(scheduler.LeaseLostError):
            await scheduler.run_with_lease(None, None, "test", None, "scheduler:test", 1)
        assert cleaned.is_set()

    asyncio.run(run())


def test_cancel_after_lock_acquisition_invalidates_session(monkeypatch):
    command = AsyncMock()
    monkeypatch.setattr(scheduler, "run_command", command)

    async def run():
        acquired = asyncio.Event()
        connection = AsyncMock()
        connection.scalar.return_value = True

        async def pending_commit():
            acquired.set()
            await asyncio.Event().wait()

        connection.commit.side_effect = pending_commit
        context = AsyncMock()
        context.__aenter__.return_value = connection
        engine = Mock()
        engine.connect.return_value = context
        running = asyncio.create_task(
            scheduler.cycle(engine, "test", scheduler.Task("test", 1, ()))
        )
        await acquired.wait()
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        connection.invalidate.assert_awaited_once()

    asyncio.run(run())
    command.assert_not_awaited()
