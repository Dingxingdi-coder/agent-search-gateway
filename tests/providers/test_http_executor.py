import io
import logging

import httpx
import pytest

from agent_search_gateway.errors import ErrorCode, ExecutionFailure, ProtocolFailure
from agent_search_gateway.models import RetryPolicy
from agent_search_gateway.observability import KeyValueFormatter, SecretRedactor, SecretValue
from agent_search_gateway.providers.http import HttpJsonExecutor
from tests.support.logging import structured_test_logger


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
    handler_stream.setFormatter(
        KeyValueFormatter(SecretRedactor([SecretValue("credential-value")]))
    )
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
            "https://endpoint-user:ENDPOINT_PASSWORD_SENTINEL@provider.example.test/search?q=QUERY_PARAMETER_SENTINEL#fragment",
            stage="search",
            headers={"Authorization": "Bearer credential-value"},
            json_body={"query": "SENSITIVE_BODY"},
        )

    assert result == {"ok": True}
    assert attempts == 3
    logged = stream.getvalue()
    lines = logged.splitlines()
    assert sum("event=http_attempt_started" in line for line in lines) == 3
    assert sum("event=http_attempt_completed" in line for line in lines) == 3
    retry_lines = [line for line in lines if "event=http_retrying" in line]
    assert len(retry_lines) == 2
    assert all(line.startswith("WARNING ") for line in retry_lines)
    assert "attempt=1" in retry_lines[0] and "delay_ms=10" in retry_lines[0]
    assert "attempt=2" in retry_lines[1] and "delay_ms=20" in retry_lines[1]
    assert sum("status=500" in line for line in lines) >= 2
    assert any("status=200" in line for line in lines)
    assert all("provider=fake" in line for line in lines)
    assert all("stage=search" in line for line in lines)
    assert all("endpoint=https://provider.example.test/search" in line for line in lines)
    assert "ENDPOINT_PASSWORD_SENTINEL" not in logged
    assert "QUERY_PARAMETER_SENTINEL" not in logged
    assert "#fragment" not in logged
    assert "credential-value" not in logged
    assert "SENSITIVE_BODY" not in logged
    assert "Request(" not in logged


async def test_http_executor_classifies_invalid_json_as_protocol_failure_without_retry() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(200, text="not-json", request=request)

    logger, stream = structured_test_logger("tests.http.invalid-json")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        executor = HttpJsonExecutor(
            client,
            RetryPolicy(3, 0.01, 0.02, 1.0),
            provider_name="fake",
            logger=logger,
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
    logged = stream.getvalue()
    assert "event=http_attempt_started" in logged
    assert "event=http_attempt_completed" in logged
    assert "status=200" in logged
    assert "event=http_failed" in logged
    assert "category=decode" in logged
    assert "not-json" not in logged


async def test_http_executor_maps_non_retryable_http_error_to_execution_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="do-not-log-response-body", request=request)

    logger, stream = structured_test_logger("tests.http.terminal-status")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        executor = HttpJsonExecutor(
            client,
            RetryPolicy(3, 0.01, 0.02, 1.0),
            provider_name="fake",
            logger=logger,
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
    logged = stream.getvalue()
    assert "event=http_failed" in logged
    assert "category=status" in logged
    assert "status=401" in logged
    assert "do-not-log-response-body" not in logged


async def test_http_executor_logs_transport_retry_without_exception_or_payload_text() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("TRANSPORT_DETAIL_SENTINEL", request=request)
        return httpx.Response(200, json={"ok": True}, request=request)

    logger, stream = structured_test_logger("tests.http.transport")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        executor = HttpJsonExecutor(
            client,
            RetryPolicy(2, 0.01, 0.02, 1.0),
            provider_name="fake",
            logger=logger,
            sleep=_no_sleep,
        )
        result = await executor.request_json(
            "POST",
            "https://provider.example.test/search",
            stage="search",
            json_body={"query": "REQUEST_BODY_SENTINEL"},
        )

    assert result == {"ok": True}
    assert attempts == 2
    logged = stream.getvalue()
    assert "event=http_retrying" in logged
    assert "category=transport" in logged
    assert "attempt=1" in logged
    assert "delay_ms=10" in logged
    assert "event=http_attempt_completed" in logged
    assert "status=200" in logged
    assert "TRANSPORT_DETAIL_SENTINEL" not in logged
    assert "REQUEST_BODY_SENTINEL" not in logged
