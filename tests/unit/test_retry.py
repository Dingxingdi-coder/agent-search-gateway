from collections.abc import Awaitable, Callable

import pytest

from agent_search_gateway.config import resolve_retry_policy
from agent_search_gateway.errors import ConfigFailure, ErrorCode, ExecutionFailure
from agent_search_gateway.retry import retry_async


async def test_retry_engine_uses_configured_attempts_and_exponential_delays_without_sleeping() -> (
    None
):
    policy = resolve_retry_policy({})
    assert policy.max_attempts == 3
    assert policy.base_delay_seconds == 0.25
    assert policy.max_delay_seconds == 2.0
    assert policy.request_timeout_seconds == 30.0

    calls = 0
    delays: list[float] = []

    async def operation() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise ExecutionFailure(ErrorCode.ALL_PROVIDERS_FAILED, "retryable")
        return "done"

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    result = await retry_async(
        policy,
        operation,
        is_retryable=lambda exc: isinstance(exc, ExecutionFailure),
        sleep=fake_sleep,
    )
    assert result == "done"
    assert calls == 3
    assert delays == [0.25, 0.5]

    non_retry_calls = 0

    async def non_retryable() -> str:
        nonlocal non_retry_calls
        non_retry_calls += 1
        raise ValueError("stop")

    with pytest.raises(ValueError, match="stop"):
        await retry_async(
            policy,
            non_retryable,
            is_retryable=lambda exc: isinstance(exc, ExecutionFailure),
            sleep=fake_sleep,
        )
    assert non_retry_calls == 1

    exhausted_calls = 0
    final_failure = ExecutionFailure(ErrorCode.ALL_PROVIDERS_FAILED, "final")

    async def exhausted() -> str:
        nonlocal exhausted_calls
        exhausted_calls += 1
        if exhausted_calls == policy.max_attempts:
            raise final_failure
        raise ExecutionFailure(ErrorCode.ALL_PROVIDERS_FAILED, "earlier")

    with pytest.raises(ExecutionFailure) as caught:
        await retry_async(
            policy,
            exhausted,
            is_retryable=lambda exc: isinstance(exc, ExecutionFailure),
            sleep=_ignore_sleep,
        )
    assert caught.value is final_failure


@pytest.mark.parametrize(
    "retry_table",
    [
        {"max_attempts": 0},
        {"base_delay_seconds": 0},
        {"max_delay_seconds": -1},
        {"request_timeout_seconds": 0},
        {"base_delay_seconds": float("nan")},
        {"max_delay_seconds": float("inf")},
        {"request_timeout_seconds": float("-inf")}
    ],
)
def test_retry_config_rejects_invalid_values(retry_table: dict[str, object]) -> None:
    with pytest.raises(ConfigFailure) as caught:
        resolve_retry_policy({"retry": retry_table})
    assert caught.value.code is ErrorCode.CONFIG_ERROR


async def _ignore_sleep(_delay: float) -> None:
    return None


Sleep = Callable[[float], Awaitable[None]]
