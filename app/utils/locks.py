from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator


class ScopeLockManager:
    """Async lock manager keyed by DHCP scope/network ID."""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._guard: asyncio.Lock | None = None
        self._guard_loop: asyncio.AbstractEventLoop | None = None

    def _get_guard(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._guard is None or self._guard_loop is not loop:
            self._locks = {}
            self._guard = asyncio.Lock()
            self._guard_loop = loop
        return self._guard

    async def _get_lock(self, scope_id: str) -> asyncio.Lock:
        async with self._get_guard():
            lock = self._locks.get(scope_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[scope_id] = lock
            return lock

    @asynccontextmanager
    async def lock(self, scope_id: str) -> AsyncIterator[None]:
        lock = await self._get_lock(scope_id)
        async with lock:
            yield


scope_locks = ScopeLockManager()
