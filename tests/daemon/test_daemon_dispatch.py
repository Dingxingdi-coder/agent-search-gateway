import asyncio
import logging
import socket
from pathlib import Path

import pytest
from _pytest.logging import LogCaptureFixture

from agent_search_gateway.daemon import ForegroundDaemon
from agent_search_gateway.errors import ConfigFailure, ErrorCode, ExecutionFailure
from agent_search_gateway.models import (
    ErrorResponse,
    KeywordSearchRequest,
    LLMSearchRequest,
    SuccessResponse,
    URLFetchRequest,
)
from agent_search_gateway.paths import RuntimePaths
from agent_search_gateway.protocol import send_request


class _FakeSearch:
    async def keyword_search(self, query: str) -> str:
        if query == "typed-failure":
            raise ExecutionFailure(ErrorCode.ALL_PROVIDERS_FAILED, "typed failure")
        if query == "unexpected":
            raise RuntimeError("sensitive unexpected detail")
        return f"keyword:{query}"

    async def llm_search(self, prompt: str) -> str:
        return f"llm:{prompt}"


class _FakeFetch:
    async def url_fetch(self, url: str, focus: str | None = None) -> str:
        return f"fetch:{url}:{focus or '-'}"


class _FakeRuntime:
    def __init__(self) -> None:
        self.search_orchestrator = _FakeSearch()
        self.fetch_orchestrator = _FakeFetch()
        self.close_calls = 0

    async def aclose(self) -> None:
        self.close_calls += 1


async def test_daemon_loads_runtime_binds_socket_and_dispatches_typed_requests(
    tmp_path: Path,
    caplog: LogCaptureFixture,
) -> None:
    paths = RuntimePaths.from_home(tmp_path)
    runtime = _FakeRuntime()
    caplog.set_level(logging.ERROR)
    daemon = ForegroundDaemon(paths, runtime_factory=lambda: runtime)
    daemon_task = asyncio.create_task(daemon.start())
    await daemon.ready.wait()

    assert paths.socket_file.exists()
    assert paths.results_dir.exists()
    assert await send_request(paths.socket_file, KeywordSearchRequest("hello")) == SuccessResponse(
        "keyword:hello"
    )
    assert await send_request(paths.socket_file, LLMSearchRequest("find")) == SuccessResponse(
        "llm:find"
    )
    assert await send_request(
        paths.socket_file,
        URLFetchRequest("https://example.com", "topic"),
    ) == SuccessResponse("fetch:https://example.com:topic")
    assert await send_request(
        paths.socket_file,
        KeywordSearchRequest("typed-failure"),
    ) == ErrorResponse(ErrorCode.ALL_PROVIDERS_FAILED, "typed failure")
    unexpected = await send_request(
        paths.socket_file,
        KeywordSearchRequest("unexpected"),
    )
    assert unexpected == ErrorResponse(ErrorCode.PROTOCOL_ERROR, "Internal daemon error")
    assert "sensitive unexpected detail" not in caplog.text

    reader, writer = await asyncio.open_unix_connection(path=paths.socket_file)
    writer.write(b'not-json\n{"type":"unknown"}\n')
    await writer.drain()
    first = await reader.readline()
    second = await reader.readline()
    writer.close()
    await writer.wait_closed()
    assert b'"error":"bad_request"' in first
    assert b'"error":"bad_request"' in second

    await daemon.stop_for_test()
    await daemon_task
    assert runtime.close_calls == 1
    assert not paths.socket_file.exists()


async def test_daemon_rejects_live_socket_and_recovers_stale_socket(tmp_path: Path) -> None:
    live_paths = RuntimePaths(
        config_file=tmp_path / "live.toml",
        socket_file=tmp_path / "live.sock",
        results_dir=tmp_path / "live-results",
    )
    live_runtime = _FakeRuntime()
    live_daemon = ForegroundDaemon(live_paths, runtime_factory=lambda: live_runtime)
    live_task = asyncio.create_task(live_daemon.start())
    await asyncio.wait_for(live_daemon.ready.wait(), timeout=1.0)

    duplicate_runtime = _FakeRuntime()
    duplicate = ForegroundDaemon(live_paths, runtime_factory=lambda: duplicate_runtime)
    try:
        with pytest.raises(ConfigFailure, match="already running"):
            await duplicate.start()
        assert duplicate_runtime.close_calls == 0
        assert await send_request(
            live_paths.socket_file,
            KeywordSearchRequest("still-live"),
        ) == SuccessResponse("keyword:still-live")
    finally:
        await live_daemon.stop_for_test()
        await live_task

    stale_paths = RuntimePaths(
        config_file=tmp_path / "stale.toml",
        socket_file=tmp_path / "stale.sock",
        results_dir=tmp_path / "stale-results",
    )
    stale_paths.socket_file.parent.mkdir(parents=True, exist_ok=True)
    stale_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale_socket.bind(str(stale_paths.socket_file))
    stale_socket.close()

    stale_runtime = _FakeRuntime()
    stale_daemon = ForegroundDaemon(stale_paths, runtime_factory=lambda: stale_runtime)
    stale_task = asyncio.create_task(stale_daemon.start())
    await asyncio.wait_for(stale_daemon.ready.wait(), timeout=1.0)
    await stale_daemon.stop_for_test()
    await stale_task
    assert stale_runtime.close_calls == 1
    assert not stale_paths.socket_file.exists()


async def test_daemon_cancellation_cleans_up_runtime_and_socket(tmp_path: Path) -> None:
    paths = RuntimePaths(
        config_file=tmp_path / "cancel.toml",
        socket_file=tmp_path / "cancel.sock",
        results_dir=tmp_path / "cancel-results",
    )
    runtime = _FakeRuntime()
    daemon = ForegroundDaemon(paths, runtime_factory=lambda: runtime)
    task = asyncio.create_task(daemon.start())
    await asyncio.wait_for(daemon.ready.wait(), timeout=1.0)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert runtime.close_calls == 1
    assert not paths.socket_file.exists()
