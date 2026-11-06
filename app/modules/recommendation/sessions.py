"""Bounded process-local continuation and idempotency storage, not chat history."""

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass, field


@dataclass
class SearchSession:
    conditions: object = None
    pages: dict = field(default_factory=dict)
    ended: set = field(default_factory=set)
    remaining: list = field(default_factory=list)
    shown: set = field(default_factory=set)
    searched: set = field(default_factory=set)
    version: int = 0


class RecommendationSessions:
    def __init__(self, ttl_seconds=1800, max_entries=256):
        self.ttl_seconds, self.max_entries = ttl_seconds, max_entries
        self.trips = OrderedDict()
        self.requests = OrderedDict()
        self.lock = asyncio.Lock()

    def prune(self):
        now = time.monotonic()
        for store in (self.trips, self.requests):
            for key in list(store):
                if now - store[key][0] > self.ttl_seconds:
                    del store[key]

    def put(self, store, key, value):
        store[key] = (time.monotonic(), value)
        store.move_to_end(key)
        while len(store) > self.max_entries:
            store.popitem(last=False)
