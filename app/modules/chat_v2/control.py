"""Task-local runtime checkpoint, absent for the synchronous internal test harness."""

from contextvars import ContextVar
from copy import deepcopy

active_control = ContextVar("chat_v2_control", default=None)


class RunControl:
    def __init__(self, store, claim):
        self.store, self.claim = store, claim
        self.operation = {"schema_version": 1, "kind": "interpretation"}

    async def checkpoint(self, stage, operation=None, update=None):
        if operation is not None:
            self.operation = {"schema_version": 1, **operation}
        if update is not None:
            self.operation = {**self.operation, **update}
        await self.store.checkpoint(self.claim, stage, deepcopy(self.operation))


async def checkpoint(stage, *, operation=None, update=None):
    control = active_control.get()
    if control is not None:
        await control.checkpoint(stage, operation, update)
