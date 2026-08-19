"""Shared HTTP JSON execution boundary for provider adapters."""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping

import httpx

from ..errors import ErrorCode, ExecutionFailure, ProtocolFailure
from ..models import RetryPolicy
from ..retry import retry_async


class _RetryableStatus(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(str(status_code))
        self.status_code = status_code


class HttpJsonExecutor:
    def __init__(
        self,
        client: httpx.AsyncClient,
        retry_policy: RetryPolicy,
        *,
        provider_name: str,
        logger: logging.Logger | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._client = client
        self._retry_policy = retry_policy
        self._provider_name = provider_name
        self._logger = logger or logging.getLogger(__name__)
        self._sleep = sleep

    async def request_json(
        self,
        method: str,
        url: str,
        *,
        stage: str,
        headers: Mapping[str, str] | None = None,
        json_body: object | None = None,
    ) -> object:
        async def operation() -> httpx.Response:
            try:
                response = await self._client.request(
                    method,
                    url,
                    headers=headers,
                    json=json_body,
                    timeout=self._retry_policy.request_timeout_seconds,
                )
            except (httpx.TimeoutException, httpx.TransportError):
                self._logger.warning(
                    "provider=%s stage=%s transport_failure",
                    self._provider_name,
                    stage,
                )
                raise

            if response.status_code in {408, 429} or response.status_code >= 500:
                self._logger.warning(
                    "provider=%s stage=%s retryable_status=%s",
                    self._provider_name,
                    stage,
                    response.status_code,
                )
                raise _RetryableStatus(response.status_code)
            return response

        try:
            response = await retry_async(
                self._retry_policy,
                operation,
                is_retryable=self._is_retryable,
                sleep=self._sleep,
            )
        except _RetryableStatus as exc:
            raise self._execution_failure(stage, f"HTTP status {exc.status_code}") from exc
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise self._execution_failure(stage, "HTTP transport failure") from exc

        if response.status_code >= 400:
            raise self._execution_failure(stage, f"HTTP status {response.status_code}")

        try:
            return response.json()
        except ValueError as exc:
            raise ProtocolFailure(
                ErrorCode.PROTOCOL_ERROR,
                f"{self._provider_name}/{stage}: response was not valid JSON",
            ) from exc

    async def aclose(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _is_retryable(exc: BaseException) -> bool:
        return isinstance(
            exc,
            _RetryableStatus | httpx.TimeoutException | httpx.TransportError,
        )

    def _execution_failure(self, stage: str, reason: str) -> ExecutionFailure:
        return ExecutionFailure(
            ErrorCode.ALL_PROVIDERS_FAILED,
            f"{self._provider_name}/{stage}: {reason}",
        )
