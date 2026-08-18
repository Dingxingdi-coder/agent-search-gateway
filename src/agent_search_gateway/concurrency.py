"""Provider quotas, singleflight, and keyed serialization primitives."""

import asyncio
from collections.abc import Awaitable, Callable, Hashable, Mapping
from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")
K = TypeVar("K", bound=Hashable)


class CapacityLease:
    def __init__(self, gate: "CapacityGate", *, acquired: bool = False) -> None:
        self._gate = gate
        self._acquired = acquired
        self._released = False

    async def __aenter__(self) -> "CapacityLease":
        if not self._acquired:
            await self._gate._acquire()
            self._acquired = True
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.release()

    async def release(self) -> None:
        if not self._acquired or self._released:
            return
        self._released = True
        await self._gate._release()


class CapacityGate:
    def __init__(self, limit: int) -> None:
        if limit <= 0:
            raise ValueError("capacity limit must be positive")
        self.limit = limit
        self.in_use = 0
        self.max_observed_in_use = 0
        self._condition = asyncio.Condition()

    def lease(self) -> CapacityLease:
        return CapacityLease(self)

    async def try_lease(self) -> CapacityLease | None:
        async with self._condition:
            if self.in_use >= self.limit:
                return None
            self._claim()
            return CapacityLease(self, acquired=True)

    async def wait_until_available(self) -> None:
        async with self._condition:
            await self._condition.wait_for(lambda: self.in_use < self.limit)

    async def _acquire(self) -> None:
        async with self._condition:
            await self._condition.wait_for(lambda: self.in_use < self.limit)
            self._claim()

    def _claim(self) -> None:
        self.in_use += 1
        self.max_observed_in_use = max(self.max_observed_in_use, self.in_use)

    async def _release(self) -> None:
        async with self._condition:
            if self.in_use <= 0:
                raise RuntimeError("capacity lease released more than once")
            self.in_use -= 1
            self._condition.notify_all()


class ProviderQuotaManager:
    def __init__(
        self,
        *,
        web_limits: Mapping[str, int],
        llm_limits: Mapping[str, int],
    ) -> None:
        self._web = {name: CapacityGate(limit) for name, limit in web_limits.items()}
        self._llm = {name: CapacityGate(limit) for name, limit in llm_limits.items()}

    def get_web(self, name: str) -> CapacityGate:
        return self._web[name]

    def get_llm(self, name: str) -> CapacityGate:
        return self._llm[name]

    async def wait_until_any_web_available(self, candidate_names: tuple[str, ...]) -> None:
        if not candidate_names:
            return
        tasks = [
            asyncio.create_task(self.get_web(name).wait_until_available())
            for name in candidate_names
        ]
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()


@dataclass(slots=True)
class _LockEntry:
    lock: asyncio.Lock
    references: int = 0


class _KeyedLockLease(Generic[K]):
    def __init__(self, pool: "PerKeyLockPool[K]", key: K) -> None:
        self._pool = pool
        self._key = key
        self._entry: _LockEntry | None = None

    async def __aenter__(self) -> None:
        self._entry = await self._pool._reserve(self._key)
        try:
            await self._entry.lock.acquire()
        except BaseException:
            await self._pool._unreserve(self._key, self._entry)
            self._entry = None
            raise

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        entry = self._entry
        if entry is None:
            return
        entry.lock.release()
        await self._pool._unreserve(self._key, entry)
        self._entry = None


class PerKeyLockPool(Generic[K]):
    def __init__(self) -> None:
        self._entries: dict[K, _LockEntry] = {}
        self._guard = asyncio.Lock()

    def acquire(self, key: K) -> _KeyedLockLease[K]:
        return _KeyedLockLease(self, key)

    async def _reserve(self, key: K) -> _LockEntry:
        async with self._guard:
            entry = self._entries.get(key)
            if entry is None:
                entry = _LockEntry(asyncio.Lock())
                self._entries[key] = entry
            entry.references += 1
            return entry

    async def _unreserve(self, key: K, entry: _LockEntry) -> None:
        async with self._guard:
            entry.references -= 1
            if entry.references == 0:
                del self._entries[key]


class SingleflightGroup(Generic[K, T]):
    def __init__(self) -> None:
        self._guard = asyncio.Lock()
        self._inflight: dict[K, asyncio.Future[T]] = {}

    async def do(self, key: K, factory: Callable[[], Awaitable[T]]) -> T:
        async with self._guard:
            future = self._inflight.get(key)
            if future is None:
                future = asyncio.get_running_loop().create_future()
                self._inflight[key] = future
                leader = True
            else:
                leader = False

        if not leader:
            return await asyncio.shield(future)

        try:
            result = await factory()
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                future.cancel()
            else:
                future.set_exception(exc)
                future.exception()
            raise
        else:
            future.set_result(result)
            return result
        finally:
            await self._cleanup(key, future)

    async def _cleanup(self, key: K, future: asyncio.Future[T]) -> None:
        async with self._guard:
            if self._inflight.get(key) is future:
                del self._inflight[key]
