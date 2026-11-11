"""Process shutdown policy shared by job entry points and scheduler children."""

import asyncio
import json
import os
import signal
import threading
import time
from contextvars import ContextVar
from dataclasses import dataclass, field

SHUTDOWN_SECONDS = 45
CHILD_TERM_SECONDS = 10
CHILD_KILL_SECONDS = 5


@dataclass
class ShutdownState:
    signum: int | None = None
    started: float = 0
    loop: asyncio.AbstractEventLoop | None = None
    task: asyncio.Task | None = None
    timer: threading.Timer | None = None
    children: set[int] = field(default_factory=set)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def signal_received(self, signum, frame):
        # Repeated signals must not cancel a child/DB cleanup a second time.
        if self.signum is not None:
            return
        self.signum, self.started = signum, time.monotonic()
        self.timer = threading.Timer(SHUTDOWN_SECONDS, self.force_exit)
        self.timer.daemon = True
        self.timer.start()
        if self.loop is not None and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self.cancel_operation)

    def cancel_operation(self):
        from jobs.runtime import report

        report({"status": "shutdown_requested", "signal": self.signum})
        if self.task is not None:
            self.task.cancel()

    def force_exit(self):
        with self.lock:
            children = list(self.children)
        try:
            for pid in children:
                signal_group(pid, signal.SIGKILL)
        finally:
            # A wedged coroutine or executor thread must not defeat the process deadline.
            hard_exit(128 + self.signum, "shutdown_deadline_exceeded")


STATE: ContextVar[ShutdownState | None] = ContextVar("job_shutdown", default=None)


def stopping() -> bool:
    state = STATE.get()
    return state is not None and state.signum is not None


def check_stopping():
    if stopping():
        raise asyncio.CancelledError


def signal_group(pid: int, signum: int):
    try:
        os.killpg(pid, signum)
    except ProcessLookupError:
        pass


def hard_exit(code: int, error_code: str):
    try:
        os.set_blocking(2, False)
        os.write(2, (json.dumps({"status": "failed", "error_code": error_code}) + "\n").encode())
    except OSError:
        pass
    os._exit(code)


async def complete_cleanup(awaitable):
    """Finish cleanup even if its caller receives an additional cancellation."""
    task = asyncio.ensure_future(awaitable)
    interrupted = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            interrupted = True
    result = task.result()
    if interrupted:
        raise asyncio.CancelledError
    return result


async def stop_child(process):
    from jobs.runtime import report

    if process.returncode is not None:
        signal_group(process.pid, signal.SIGKILL)
        return
    report({"status": "child_stopping", "pid": process.pid})
    signal_group(process.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), CHILD_TERM_SECONDS)
    except TimeoutError:
        report({"status": "child_killing", "pid": process.pid})
        signal_group(process.pid, signal.SIGKILL)
        try:
            await asyncio.wait_for(process.wait(), CHILD_KILL_SECONDS)
        except TimeoutError:
            # Do not unlock and schedule a new cycle with an unaccounted-for child.
            hard_exit(1, "child_reap_deadline_exceeded")
    signal_group(process.pid, signal.SIGKILL)
    report({"status": "child_reaped", "pid": process.pid, "exit_code": process.returncode})
