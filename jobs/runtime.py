"""Shared job paths, JSON reporting and process exit handling."""

import asyncio
import json
import signal
import sys
import time
from pathlib import Path

from jobs.shutdown import STATE, ShutdownState

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REFERENCE_DATA = PROJECT_ROOT / "data"
EXIT_CODES = {"success": 0, "failed": 1, "partial": 2, "skipped": 3, "paused_provider_limit": 4}


def report(result: dict) -> None:
    # Parent and children share stdout. Keep each short JSON record and its newline
    # in one write; print(..., flush=True) can interleave them under python -u.
    sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def finish(result: dict) -> int:
    report(result)
    code = EXIT_CODES.get(result["status"], 1)
    if code == 0 and result.get("publication", {}).get("status") in {"failed", "deferred"}:
        return EXIT_CODES["partial"]
    return code


def run_cli(operation, *args) -> None:
    state = ShutdownState()
    token = STATE.set(state)
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    for sig in previous:
        signal.signal(sig, state.signal_received)

    async def run():
        state.loop = asyncio.get_running_loop()
        state.task = asyncio.current_task()
        if state.signum is not None:
            report({"status": "shutdown_requested", "signal": state.signum})
            return 128 + state.signum
        try:
            return await operation(*args)
        except asyncio.CancelledError:
            if state.signum is None:
                raise
            return 128 + state.signum

    try:
        try:
            code = asyncio.run(run())
        except KeyboardInterrupt:
            code = 130
        except asyncio.CancelledError:
            code = finish({"status": "failed", "error_code": "job_cancelled"})
        except Exception:
            # Arbitrary exception text can contain credentials or provider URLs.
            code = finish({"status": "failed", "error_code": "configuration_or_database_error"})
        if state.signum is not None:
            code = 128 + state.signum
            report(
                {
                    "status": "shutdown_completed",
                    "exit_code": code,
                    "elapsed_seconds": round(time.monotonic() - state.started, 3),
                }
            )
    finally:
        if state.timer is not None:
            state.timer.cancel()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        STATE.reset(token)
    raise SystemExit(code)
