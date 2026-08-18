import asyncio
import logging
from pathlib import Path

from _pytest.logging import LogCaptureFixture

from agent_search_gateway.daemon import ForegroundDaemon
from agent_search_gateway.errors import ErrorCode, ExecutionFailure
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
