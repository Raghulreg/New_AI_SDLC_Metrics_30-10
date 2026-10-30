from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Hashable


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SourceStore:
    """In-memory TTL cache of *successful* real source responses.

    Failures are never cached and a failed refresh never falls back to an older
    value; callers receive the error and can read `last_success` for display.
    """

    def __init__(self, ttl_seconds: float):
        self.ttl = ttl_seconds
        self._data: dict[Hashable, tuple[datetime, Any]] = {}
        self._last_success: dict[Hashable, datetime] = {}
        self._inflight: dict[Hashable, asyncio.Task] = {}

    def last_success(self, key: Hashable | None) -> datetime | None:
        return self._last_success.get(key) if key is not None else None

    async def get(self, key: Hashable, fetcher: Callable[[], Awaitable[Any]], force: bool = False) -> tuple[Any, datetime]:
        if key in self._inflight:
            return await asyncio.shield(self._inflight[key])
        if not force and key in self._data:
            fetched_at, value = self._data[key]
            if (utcnow() - fetched_at).total_seconds() < self.ttl:
                return value, fetched_at
        self._data.pop(key, None)

        async def run() -> tuple[Any, datetime]:
            value = await fetcher()
            fetched_at = utcnow()
            self._data[key] = (fetched_at, value)
            self._last_success[key] = fetched_at
            return value, fetched_at

        task = asyncio.ensure_future(run())
        self._inflight[key] = task
        try:
            return await asyncio.shield(task)
        finally:
            if task.done():
                self._inflight.pop(key, None)
            else:
                task.add_done_callback(lambda _t: self._inflight.pop(key, None))
