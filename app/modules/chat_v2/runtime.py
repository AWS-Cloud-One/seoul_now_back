"""Bounded in-process workers; DB admission survives HTTP and process lifetimes."""

import asyncio
import logging
from contextlib import suppress

from app.modules.chat_v2.control import RunControl, active_control
from app.modules.chat_v2.storage_contracts import StorageRejected

logger = logging.getLogger(__name__)


class ChatRuntime:
    def __init__(self, store, execution, *, concurrency=2, poll_seconds=0.5, heartbeat_seconds=5):
        if not 1 <= concurrency <= 4 or not 0 < heartbeat_seconds < 15 or poll_seconds <= 0:
            raise ValueError("Invalid runtime limits")
        self.store, self.execution = store, execution
        self.concurrency, self.poll_seconds = concurrency, poll_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self.jobs = set()
        self.loop_task = None

    async def submit(self, credential, conversation_id, request, *, peer="internal"):
        return await self.store.admit(
            credential, conversation_id, request, dialogue=True, queued=True, peer=peer
        )

    async def tick(self):
        await self.store.recover()
        await self.store.cleanup()
        if self.store.budgets:
            await self.store.budgets.cleanup()
        while len(self.jobs) < self.concurrency:
            item = await self.store.claim_next()
            if item is None:
                break
            task = asyncio.create_task(self._execute(*item))
            self.jobs.add(task)
            task.add_done_callback(self.jobs.discard)

    async def _heartbeat(self, claim):
        while True:
            await asyncio.sleep(self.heartbeat_seconds)
            if not await self.store.renew(claim):
                return

    async def _execute(self, claim, request, source):
        token = active_control.set(RunControl(self.store, claim))
        work = asyncio.create_task(self.execution.execute_claim(claim, request, source))
        heart = asyncio.create_task(self._heartbeat(claim))
        try:
            remaining = max(0, (claim.deadline_at - self.store.clock()).total_seconds())
            async with asyncio.timeout(remaining):
                done, _ = await asyncio.wait([work, heart], return_when=asyncio.FIRST_COMPLETED)
                if work in done:
                    result = await work
                    if result.status == "reconciling":
                        # Same locked writer proof as recovery; never retry a business write.
                        await self.store.stop_claim(claim)
                else:
                    await heart  # a lost fence/DB connection also stops local work
        except TimeoutError:
            await self._stop(claim, "DEADLINE_EXCEEDED")
        except asyncio.CancelledError:
            await self._stop(claim, "INTERRUPTED")
            raise
        except StorageRejected:
            await self._stop(claim, "INTERRUPTED")
        except Exception:
            logger.warning(
                "chat_v2_execution_failed", extra={"run_id": str(claim.context.identity.run_id)}
            )
            await self._stop(claim, "INTERNAL_ERROR")
        finally:
            work.cancel()
            heart.cancel()
            await asyncio.gather(work, heart, return_exceptions=True)
            active_control.reset(token)

    async def _stop(self, claim, code):
        try:
            await self.store.stop_claim(claim, code)
        except Exception:
            # DB uncertainty remains occupied; lease recovery owns the eventual answer.
            logger.warning(
                "chat_v2_reconciliation_pending",
                extra={"run_id": str(claim.context.identity.run_id)},
            )

    async def _loop(self):
        while True:
            try:
                await self.tick()
            except Exception:
                logger.warning("chat_v2_runtime_poll_failed")
            await asyncio.sleep(self.poll_seconds)

    async def start(self):
        if self.loop_task is not None:
            raise RuntimeError("Runtime already started")
        self.loop_task = asyncio.create_task(self._loop())

    async def close(self):
        if self.loop_task is not None:
            self.loop_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.loop_task
            self.loop_task = None
        jobs = list(self.jobs)
        for task in jobs:
            task.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
