import asyncio
import logging
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from agent_search_gateway.daemon import ForegroundDaemon
from agent_search_gateway.errors import ConfigFailure, ErrorCode, ExecutionFailure
from agent_search_gateway.models import ErrorResponse, KeywordSearchRequest, SuccessResponse
from agent_search_gateway.observability import SecretValue, configure_debug_logging, log_event
from agent_search_gateway.paths import RuntimePaths
from agent_search_gateway.protocol import send_request
from agent_search_gateway.runtime import Runtime


class _Search:
    async def keyword_search(self, query: str, *, request_id: str) -> str:
        return query

    async def llm_search(self, prompt: str, *, request_id: str) -> str:
        return prompt


class _Fetch:
    async def url_fetch(self, url: str, focus: str | None = None) -> str:
        return url


class _Runtime:
    def __init__(self) -> None:
        self.search_orchestrator = _Search()
        self.fetch_orchestrator = _Fetch()
        self.close_calls = 0

    async def aclose(self) -> None:
        self.close_calls += 1


async def _run_debug_daemon(paths: RuntimePaths) -> str:
    stderr = StringIO()
    session = configure_debug_logging(paths.debug_log_file, stderr=stderr)
    runtime = _Runtime()
    daemon = ForegroundDaemon(
        paths,
        runtime_factory=lambda: runtime,
        debug=True,
        logging_session=session,
    )
    task = asyncio.create_task(daemon.start())
    await daemon.ready.wait()
    await daemon.stop_for_test()
    await task
    session.close()
    assert runtime.close_calls == 1
    return paths.debug_log_file.read_text(encoding="utf-8")


async def test_debug_daemon_writes_exact_session_markers_and_appends(tmp_path: Path) -> None:
    paths = RuntimePaths.from_home(tmp_path)

    first = await _run_debug_daemon(paths)
    assert first.count("event=session_started") == 1
    assert first.count("event=session_stopped") == 1
    assert "debug=true" in first
    assert "pid=" in first

    second = await _run_debug_daemon(paths)
    assert second.count("event=session_started") == 2
    assert second.count("event=session_stopped") == 2


async def test_normal_daemon_does_not_create_debug_log(tmp_path: Path) -> None:
    paths = RuntimePaths.from_home(tmp_path)
    runtime = _Runtime()
    daemon = ForegroundDaemon(paths, runtime_factory=lambda: runtime)
    task = asyncio.create_task(daemon.start())
    await daemon.ready.wait()
    await daemon.stop_for_test()
    await task

    assert not paths.debug_log_file.exists()


async def test_runtime_failure_before_bind_has_no_successful_session_marker(tmp_path: Path) -> None:
    paths = RuntimePaths.from_home(tmp_path)
    session = configure_debug_logging(paths.debug_log_file, stderr=StringIO())

    def fail_runtime() -> _Runtime:
        raise ConfigFailure(ErrorCode.CONFIG_ERROR, "runtime failed")

    daemon = ForegroundDaemon(
        paths,
        runtime_factory=fail_runtime,
        debug=True,
        logging_session=session,
    )
    with pytest.raises(ConfigFailure, match="runtime failed"):
        await daemon.start()
    session.close()

    text = paths.debug_log_file.read_text(encoding="utf-8")
    assert "event=session_started" not in text
    assert "event=session_stopped" not in text


async def test_default_runtime_registers_resolved_secrets_before_runtime_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = RuntimePaths.from_home(tmp_path)
    first_secret = SecretValue("web-secret-value")
    second_secret = SecretValue("llm-secret-value")
    config = SimpleNamespace(
        web=SimpleNamespace(
            providers=(
                SimpleNamespace(secret=first_secret),
                SimpleNamespace(secret=None),
            )
        ),
        llm=SimpleNamespace(providers=(SimpleNamespace(secret=second_secret),)),
    )
    registry = object()
    runtime = _Runtime()

    monkeypatch.setattr("agent_search_gateway.daemon.build_default_registry", lambda: registry)
    monkeypatch.setattr("agent_search_gateway.daemon.load_toml", lambda path: {})
    monkeypatch.setattr(
        "agent_search_gateway.daemon.resolve_config",
        lambda data, received_registry, environ: config,
    )

    def fake_build(
        config_value: object,
        runtime_paths: RuntimePaths,
        *,
        registry: object,
    ) -> _Runtime:
        log_event(
            logging.getLogger("agent_search_gateway.runtime"),
            logging.DEBUG,
            "runtime_probe",
            detail=f"{first_secret.reveal()} {second_secret.reveal()}",
        )
        return runtime

    monkeypatch.setattr(Runtime, "build", staticmethod(fake_build))
    session = configure_debug_logging(paths.debug_log_file, stderr=StringIO())
    daemon = ForegroundDaemon(paths, debug=True, logging_session=session, environ={})
    task = asyncio.create_task(daemon.start())
    await daemon.ready.wait()
    await daemon.stop_for_test()
    await task
    session.close()

    text = paths.debug_log_file.read_text(encoding="utf-8")
    assert "web-secret-value" not in text
    assert "llm-secret-value" not in text
    assert "detail=\"<redacted> <redacted>\"" in text


async def test_debug_workflow_lifecycle_logs_are_correlated_and_payload_safe(
    tmp_path: Path,
) -> None:
    class LifecycleSearch:
        async def keyword_search(self, query: str, *, request_id: str) -> str:
            if query == "typed":
                raise ExecutionFailure(ErrorCode.ALL_PROVIDERS_FAILED, "typed failure")
            if query == "boom":
                raise RuntimeError("debug internal detail")
            return "result"

        async def llm_search(self, prompt: str, *, request_id: str) -> str:
            return "unused"

    paths = RuntimePaths.from_home(tmp_path)
    runtime = _Runtime()
    runtime.search_orchestrator = cast(_Search, LifecycleSearch())
    session = configure_debug_logging(paths.debug_log_file, stderr=StringIO())
    ids = iter(["11111111", "22222222", "33333333"])
    daemon = ForegroundDaemon(
        paths,
        runtime_factory=lambda: runtime,
        request_id_factory=ids.__next__,
        debug=True,
        logging_session=session,
    )
    task = asyncio.create_task(daemon.start())
    await daemon.ready.wait()

    assert await send_request(
        paths.socket_file,
        KeywordSearchRequest("QUERY_BODY_SENTINEL"),
    ) == SuccessResponse("result")
    assert await send_request(paths.socket_file, KeywordSearchRequest("typed")) == ErrorResponse(
        ErrorCode.ALL_PROVIDERS_FAILED,
        "typed failure",
    )
    assert await send_request(paths.socket_file, KeywordSearchRequest("boom")) == ErrorResponse(
        ErrorCode.PROTOCOL_ERROR,
        "Internal daemon error",
    )

    await daemon.stop_for_test()
    await task
    session.close()

    text = paths.debug_log_file.read_text(encoding="utf-8")
    assert "QUERY_BODY_SENTINEL" not in text
    first = [line for line in text.splitlines() if "request=11111111" in line]
    second = [line for line in text.splitlines() if "request=22222222" in line]
    third = [line for line in text.splitlines() if "request=33333333" in line]
    assert any("event=workflow_started" in line for line in first)
    assert any(
        "event=workflow_completed" in line and "elapsed_ms=" in line for line in first
    )
    assert any(
        "event=workflow_failed" in line and "error_code=all_providers_failed" in line
        for line in second
    )
    assert all("traceback=" not in line for line in second)
    assert any(
        "event=workflow_failed" in line and "error_type=RuntimeError" in line
        for line in third
    )
    assert any("traceback=" in line for line in third)
