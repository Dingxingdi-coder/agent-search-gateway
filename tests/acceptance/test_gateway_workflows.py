import asyncio
import io
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from agent_search_gateway.cli import build_parser, run_command
from agent_search_gateway.daemon import ForegroundDaemon
from agent_search_gateway.errors import ErrorCode
from agent_search_gateway.models import (
    ErrorResponse,
    KeywordSearchRequest,
    LLMSearchRequest,
    PaperSearchRequest,
    ShutdownRequest,
    SuccessResponse,
    URLFetchRequest,
)
from agent_search_gateway.paths import RuntimePaths
from agent_search_gateway.protocol import send_request
from tests.support.acceptance import build_acceptance_runtime

_DAEMON_TIMEOUT_SECONDS = 2.0


async def _start_daemon(daemon: ForegroundDaemon) -> asyncio.Task[None]:
    """Start a daemon with a bounded readiness wait and surface startup failures."""
    daemon_task = asyncio.create_task(daemon.start())
    ready_task = asyncio.create_task(daemon.ready.wait())
    done, pending = await asyncio.wait(
        {daemon_task, ready_task},
        timeout=_DAEMON_TIMEOUT_SECONDS,
        return_when=asyncio.FIRST_COMPLETED,
    )
    if ready_task in done and daemon.ready.is_set():
        return daemon_task

    ready_task.cancel()
    await asyncio.gather(ready_task, return_exceptions=True)
    if daemon_task in done:
        await daemon_task
        raise AssertionError("daemon exited before becoming ready")

    for pending_task in pending:
        pending_task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    raise AssertionError("daemon did not become ready")


@asynccontextmanager
async def _running_daemon(daemon: ForegroundDaemon) -> AsyncIterator[asyncio.Task[None]]:
    """Run a daemon and guarantee bounded cleanup when an assertion fails."""
    daemon_task = await _start_daemon(daemon)
    try:
        yield daemon_task
    finally:
        if not daemon_task.done():
            await asyncio.wait_for(daemon.stop_for_test(), timeout=_DAEMON_TIMEOUT_SECONDS)
        await asyncio.wait_for(asyncio.shield(daemon_task), timeout=_DAEMON_TIMEOUT_SECONDS)


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
    async with _running_daemon(daemon) as daemon_task:
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

        paper_stdout = io.StringIO()
        paper_stderr = io.StringIO()
        paper_args = build_parser().parse_args(["paper-search", "academic topic"])
        assert (
            await run_command(
                paper_args,
                paths,
                stdout=paper_stdout,
                stderr=paper_stderr,
            )
            == 0
        )
        assert paper_stderr.getvalue() == ""
        paper_path = Path(paper_stdout.getvalue().strip())
        assert paper_path.name.startswith("paper-")
        assert paper_path.name.endswith(".jsonl")
        assert len(paper_path.stem.removeprefix("paper-")) == 8
        paper_lines = [
            json.loads(line) for line in paper_path.read_text(encoding="utf-8").splitlines()
        ]
        assert [line["title"] for line in paper_lines] == [
            "Direct Academic Paper",
            "Unique CORE Paper",
        ]
        assert all("type" not in line for line in paper_lines)
        assert paper_lines[0]["sources"] == ["openalex", "core"]
        assert paper_lines[0]["pdf_url"] == "https://repository.example/paper.pdf"
        assert paper_lines[0]["is_open_access"] is True
        assert runtime.oa_resolver.calls == ["10.1000/acceptance-shared"]

        paper_fetch = await send_request(
            paths.socket_file,
            URLFetchRequest("https://example.com/paper", None),
        )
        assert paper_fetch == SuccessResponse("Full article content")
        assert [str(url) for url in runtime.fetch_provider.calls] == [
            "https://example.com/article",
            "https://example.com/paper",
        ]
        pdf_fetch = await send_request(
            paths.socket_file,
            URLFetchRequest("https://repository.example/paper.pdf", None),
        )
        assert pdf_fetch == ErrorResponse(
            ErrorCode.URL_NOT_ADMITTED,
            "URL was not admitted by search",
        )

        mixed_stdout = io.StringIO()
        mixed_stderr = io.StringIO()
        mixed_args = build_parser().parse_args(
            ["llm-search", "find web and papers", "--scope", "all"]
        )
        assert (
            await run_command(
                mixed_args,
                paths,
                stdout=mixed_stdout,
                stderr=mixed_stderr,
            )
            == 0
        )
        assert mixed_stderr.getvalue() == ""
        mixed_path = Path(mixed_stdout.getvalue().strip())
        assert mixed_path.name.startswith("llm-")
        mixed_lines = [
            json.loads(line) for line in mixed_path.read_text(encoding="utf-8").splitlines()
        ]
        assert [line["type"] for line in mixed_lines] == ["web", "paper"]
        assert mixed_lines[0]["url"] == "https://example.com/llm"
        assert mixed_lines[1]["title"] == "LLM Academic Paper"

        assert await send_request(paths.socket_file, ShutdownRequest()) == SuccessResponse(
            "Daemon stopped."
        )
        await daemon_task
        assert not paths.socket_file.exists()

    fresh_runtime = build_acceptance_runtime(paths)
    fresh_daemon = ForegroundDaemon(paths, runtime_factory=lambda: fresh_runtime)
    async with _running_daemon(fresh_daemon) as fresh_task:
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


async def test_failing_direct_paper_search_writes_no_result_file(tmp_path: Path) -> None:
    paths = RuntimePaths(
        config_file=tmp_path / "config.toml",
        socket_file=tmp_path / "gateway.sock",
        results_dir=tmp_path / "results",
    )
    runtime = build_acceptance_runtime(paths)
    failing_provider = next(
        provider for provider in runtime.academic_providers if provider.name == "crossref"
    )
    runtime.paper_search_orchestrator.providers = (failing_provider,)
    daemon = ForegroundDaemon(paths, runtime_factory=lambda: runtime)
    async with _running_daemon(daemon) as daemon_task:
        response = await send_request(paths.socket_file, PaperSearchRequest("failing topic"))
        assert response == ErrorResponse(
            ErrorCode.ALL_PROVIDERS_FAILED,
            "All academic search provider pipelines failed",
        )
        assert not list(paths.results_dir.glob("paper-*.jsonl"))

        assert await send_request(paths.socket_file, ShutdownRequest()) == SuccessResponse(
            "Daemon stopped."
        )
        await daemon_task
