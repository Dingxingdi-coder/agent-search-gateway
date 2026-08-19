"""Event-driven concurrency helpers for tests."""

import asyncio
from typing import TypeVar

T = TypeVar("T")


class ControlledProvider:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.active = 0
        self.max_active = 0
        self.calls = 0

    async def run(self, result: T) -> T:
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.entered.set()
        try:
            await self.release.wait()
            return result
        finally:
            self.active -= 1
