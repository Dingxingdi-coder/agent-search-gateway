"""Shared HTTP JSON execution boundary for provider adapters."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping

import httpx

from ..errors import ErrorCode, ExecutionFailure, ProtocolFailure
from ..models import RetryPolicy
from ..observability import elapsed_ms, http_endpoint_for_log, log_event
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
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._retry_policy = retry_policy
        self._provider_name = provider_name
        self._logger = logger or logging.getLogger(__name__)
        self._sleep = sleep
        self._monotonic = monotonic

    async def request_json(
        self,
        method: str,
        url: str,
        *,
        stage: str,
        headers: Mapping[str, str] | None = None,
        json_body: object | None = None,
    ) -> object:
        attempt = 0
        attempt_started = self._monotonic()
        log_endpoint = http_endpoint_for_log(url)

        def before_attempt(current_attempt: int) -> None:
            nonlocal attempt, attempt_started
            attempt = current_attempt
            attempt_started = self._monotonic()
            log_event(
                self._logger,
                logging.DEBUG,
                "http_attempt_started",
                provider=self._provider_name,
                stage=stage,
                endpoint=log_endpoint,
                attempt=attempt,
            )

        def on_retry(current_attempt: int, exc: BaseException, delay: float) -> None:
            delay_ms = max(0, int(delay * 1000))
            attempt_elapsed_ms = elapsed_ms(self._monotonic, attempt_started)
            if isinstance(exc, _RetryableStatus):
                log_event(
                    self._logger,
                    logging.WARNING,
                    "http_retrying",
                    provider=self._provider_name,
                    stage=stage,
                    endpoint=log_endpoint,
                    attempt=current_attempt,
                    delay_ms=delay_ms,
                    elapsed_ms=attempt_elapsed_ms,
                    category="status",
                    status=exc.status_code,
                )
                return
            log_event(
                self._logger,
                logging.WARNING,
                "http_retrying",
                provider=self._provider_name,
                stage=stage,
                endpoint=log_endpoint,
                attempt=current_attempt,
                delay_ms=delay_ms,
                elapsed_ms=attempt_elapsed_ms,
                category="transport",
            )

        async def operation() -> httpx.Response:
            response = await self._client.request(
                method,
                url,
                headers=headers,
                json=json_body,
                timeout=self._retry_policy.request_timeout_seconds,
            )
            log_event(
                self._logger,
                logging.DEBUG,
                "http_attempt_completed",
                provider=self._provider_name,
                stage=stage,
                endpoint=log_endpoint,
                attempt=attempt,
                status=response.status_code,
                elapsed_ms=elapsed_ms(self._monotonic, attempt_started),
            )
            if response.status_code in {408, 429} or response.status_code >= 500:
                raise _RetryableStatus(response.status_code)
            return response

        try:
            response = await retry_async(
                self._retry_policy,
                operation,
                is_retryable=self._is_retryable,
                sleep=self._sleep,
                before_attempt=before_attempt,
                on_retry=on_retry,
            )
        except _RetryableStatus as exc:
            self._log_failed(stage, log_endpoint, attempt, "status", status=exc.status_code)
            raise self._execution_failure(stage, f"HTTP status {exc.status_code}") from exc
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            self._log_failed(stage, log_endpoint, attempt, "transport")
            raise self._execution_failure(stage, "HTTP transport failure") from exc

        if response.status_code >= 400:
            self._log_failed(stage, log_endpoint, attempt, "status", status=response.status_code)
            raise self._execution_failure(stage, f"HTTP status {response.status_code}")

        try:
            return response.json()
        except ValueError as exc:
            self._log_failed(stage, log_endpoint, attempt, "decode")
            raise ProtocolFailure(
                ErrorCode.PROTOCOL_ERROR,
                f"{self._provider_name}/{stage}: response was not valid JSON",
            ) from exc

    def _log_failed(
        self,
        stage: str,
        endpoint: str,
        attempt: int,
        category: str,
        *,
        status: int | None = None,
    ) -> None:
        if status is None:
            log_event(
                self._logger,
                logging.DEBUG,
                "http_failed",
                provider=self._provider_name,
                stage=stage,
                endpoint=endpoint,
                attempt=attempt,
                category=category,
            )
            return
        log_event(
            self._logger,
            logging.DEBUG,
            "http_failed",
            provider=self._provider_name,
            stage=stage,
            endpoint=endpoint,
            attempt=attempt,
            category=category,
            status=status,
        )

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
