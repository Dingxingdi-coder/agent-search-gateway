import io
import logging

import httpx
import pytest

from agent_search_gateway.errors import ErrorCode, ExecutionFailure, ProtocolFailure
from agent_search_gateway.models import RetryPolicy
from agent_search_gateway.observability import SecretRedactingFilter, SecretValue
from agent_search_gateway.providers.http import HttpJsonExecutor


async def _no_sleep(_delay: float) -> None:
    return None


async def test_http_executor_retries_retryable_status_and_hides_sensitive_payloads() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(500, text="SENSITIVE_BODY", request=request)
        return httpx.Response(200, json={"ok": True}, request=request)

    stream = io.StringIO()
    logger = logging.getLogger("test.http.executor")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    handler_stream = logging.StreamHandler(stream)
    handler_stream.addFilter(SecretRedactingFilter([SecretValue("credential-value")]))
    logger.addHandler(handler_stream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        executor = HttpJsonExecutor(
            client,
            RetryPolicy(3, 0.01, 0.02, 1.0),
            provider_name="fake",
            logger=logger,
            sleep=_no_sleep,
        )
        result = await executor.request_json(
            "POST",
            "https://provider.example.test/search",
            stage="search",
            headers={"Authorization": "Bearer credential-value"},
            json_body={"query": "SENSITIVE_BODY"},
        )

    assert result == {"ok": True}
    assert attempts == 3
    logged = stream.getvalue()
    assert "fake" in logged
    assert "search" in logged
    assert "credential-value" not in logged
    assert "SENSITIVE_BODY" not in logged


async def test_http_executor_classifies_invalid_json_as_protocol_failure_without_retry() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(200, text="not-json", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        executor = HttpJsonExecutor(
            client,
            RetryPolicy(3, 0.01, 0.02, 1.0),
            provider_name="fake",
            sleep=_no_sleep,
        )
        with pytest.raises(ProtocolFailure) as caught:
            await executor.request_json(
                "GET",
                "https://provider.example.test/data",
                stage="fetch",
            )

    assert caught.value.code is ErrorCode.PROTOCOL_ERROR
    assert attempts == 1


async def test_http_executor_maps_non_retryable_http_error_to_execution_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="do-not-log-response-body", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        executor = HttpJsonExecutor(
            client,
            RetryPolicy(3, 0.01, 0.02, 1.0),
            provider_name="fake",
            sleep=_no_sleep,
        )
        with pytest.raises(ExecutionFailure) as caught:
            await executor.request_json(
                "GET",
                "https://provider.example.test/data",
                stage="fetch",
            )

    assert caught.value.code is ErrorCode.ALL_PROVIDERS_FAILED
    assert "do-not-log-response-body" not in caught.value.message
