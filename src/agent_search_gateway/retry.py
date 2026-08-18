"""Generic configurable exponential retry engine."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

from .models import RetryPolicy

T = TypeVar("T")


async def retry_async(
    policy: RetryPolicy,
    operation: Callable[[], Awaitable[T]],
    *,
    is_retryable: Callable[[BaseException], bool],
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return await operation()
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                raise
            if attempt == policy.max_attempts or not is_retryable(exc):
                raise
            delay = min(
                policy.base_delay_seconds * (2 ** (attempt - 1)),
                policy.max_delay_seconds,
            )
            await sleep(delay)
    raise RuntimeError("retry loop exhausted unexpectedly")
