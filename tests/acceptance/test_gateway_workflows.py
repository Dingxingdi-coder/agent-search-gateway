import json
from pathlib import Path

from agent_search_gateway.daemon import ForegroundDaemon
from agent_search_gateway.errors import ErrorCode
from agent_search_gateway.models import (
    ErrorResponse,
    KeywordSearchRequest,
    LLMSearchRequest,
    ShutdownRequest,
    SuccessResponse,
    URLFetchRequest,
)
from agent_search_gateway.paths import RuntimePaths
from agent_search_gateway.protocol import send_request
from tests.support.acceptance import build_acceptance_runtime


async def test_real_socket_workflows_match_public_contract_without_network(
    tmp_path: Path,
) -> None:
    paths = RuntimePaths(
        config_file=tmp_path / "config.toml",
        socket_file=tmp_path / "gateway.sock",
        results_dir=tmp_path / "results",
    )
    runtime = build_acceptance_runtime(paths)
    daemon = ForegroundDaemon(paths, runtime_factory=lambda: runtime)
    daemon_task = __import__("asyncio").create_task(daemon.start())
    await daemon.ready.wait()

    keyword = await send_request(paths.socket_file, KeywordSearchRequest("find article"))
    assert isinstance(keyword, SuccessResponse)
    keyword_path = Path(keyword.text)
    assert keyword_path.exists()
    keyword_lines = keyword_path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line) for line in keyword_lines] == [
        {"url": "https://example.com/article", "abstract": "Keyword abstract"}
    ]
    assert all(set(json.loads(line)) == {"url", "abstract"} for line in keyword_lines)

    full = await send_request(
        paths.socket_file,
        URLFetchRequest("https://example.com/article", None),
    )
    assert full == SuccessResponse("Full article content")
    assert len(runtime.fetch_provider.calls) == 1

    focused = await send_request(
        paths.socket_file,
        URLFetchRequest("https://example.com/article", "pricing"),
    )
    assert focused == SuccessResponse("Focused summary: pricing")
    assert len(runtime.fetch_provider.calls) == 1

    llm = await send_request(paths.socket_file, LLMSearchRequest("find llm result"))
    assert isinstance(llm, SuccessResponse)
    llm_lines = Path(llm.text).read_text(encoding="utf-8").splitlines()
    assert [json.loads(line) for line in llm_lines] == [
        {"url": "https://example.com/llm", "abstract": "LLM abstract"}
    ]

    assert await send_request(paths.socket_file, ShutdownRequest()) == SuccessResponse(
        "Daemon stopped."
    )
    await daemon_task
    assert not paths.socket_file.exists()

    fresh_runtime = build_acceptance_runtime(paths)
    fresh_daemon = ForegroundDaemon(paths, runtime_factory=lambda: fresh_runtime)
    fresh_task = __import__("asyncio").create_task(fresh_daemon.start())
    await fresh_daemon.ready.wait()

    after_restart = await send_request(
        paths.socket_file,
        URLFetchRequest("https://example.com/article", None),
    )
    assert after_restart == ErrorResponse(
        ErrorCode.URL_NOT_ADMITTED,
        "URL was not admitted by search",
    )
    assert fresh_runtime.fetch_provider.calls == []

    assert await send_request(paths.socket_file, ShutdownRequest()) == SuccessResponse(
        "Daemon stopped."
    )
    await fresh_task
    assert not paths.socket_file.exists()
