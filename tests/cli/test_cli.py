from io import StringIO
from pathlib import Path

from agent_search_gateway.cli import build_parser, run_command
from agent_search_gateway.errors import DaemonUnavailable, ErrorCode, ProtocolFailure
from agent_search_gateway.models import (
    ErrorResponse,
    KeywordSearchRequest,
    LLMSearchRequest,
    Request,
    Response,
    ShutdownRequest,
    SuccessResponse,
    URLFetchRequest,
)
from agent_search_gateway.paths import RuntimePaths


class _FakeDaemon:
    def __init__(self) -> None:
        self.start_calls = 0

    async def start(self) -> None:
        self.start_calls += 1


async def test_cli_renders_exact_stdout_stderr_and_exit_codes(tmp_path: Path) -> None:
    parser = build_parser()
    for argv in (
        ["start"],
        ["stop"],
        ["keyword-search", "query"],
        ["llm-search", "prompt"],
        ["url-fetch", "https://example.com"],
        ["url-fetch", "https://example.com", "focus"],
    ):
        assert parser.parse_args(argv).command == argv[0]

    paths = RuntimePaths(
        config_file=tmp_path / "config.toml",
        socket_file=tmp_path / "gateway.sock",
        results_dir=tmp_path / "results",
    )
    calls: list[Request] = []
    response: Response = SuccessResponse("result text")
    missing = False
    protocol_failure = False

    async def client(_socket_path: Path, request: Request) -> Response:
        nonlocal response
        calls.append(request)
        if missing:
            raise DaemonUnavailable("missing")
        if protocol_failure:
            raise ProtocolFailure(ErrorCode.PROTOCOL_ERROR, "bad local protocol")
        return response

    async def invoke(argv: list[str]) -> tuple[int, str, str]:
        stdout = StringIO()
        stderr = StringIO()
        code = await run_command(
            parser.parse_args(argv),
            paths,
            client=client,
            daemon_factory=lambda _paths: _FakeDaemon(),
            stdout=stdout,
            stderr=stderr,
        )
        return code, stdout.getvalue(), stderr.getvalue()

    before = len(calls)
    assert await invoke(["keyword-search", "   "]) == (
        1,
        "",
        "Query must not be empty\n",
    )
    assert await invoke(["llm-search", "\t "]) == (
        1,
        "",
        "Prompt must not be empty\n",
    )
    assert await invoke(["url-fetch", "ftp://example.com"]) == (
        1,
        "",
        "URL must be a valid HTTP or HTTPS URL\n",
    )
    assert len(calls) == before

    missing = True
    assert await invoke(["keyword-search", "query"]) == (
        1,
        "",
        "Start the daemon with: agent-search-gateway start\n",
    )
    assert await invoke(["stop"]) == (0, "Daemon is not running.\n", "")
    missing = False

    response = SuccessResponse("/tmp/results.jsonl")
    assert await invoke(["keyword-search", "  query  "]) == (
        0,
        "/tmp/results.jsonl\n",
        "",
    )
    keyword_request = calls[-1]
    assert isinstance(keyword_request, KeywordSearchRequest)
    assert keyword_request.query == "query"

    response = SuccessResponse("llm result")
    assert await invoke(["llm-search", "  prompt  "]) == (0, "llm result\n", "")
    llm_request = calls[-1]
    assert isinstance(llm_request, LLMSearchRequest)
    assert llm_request.prompt == "prompt"

    response = SuccessResponse("content")
    assert await invoke(["url-fetch", " https://EXAMPLE.com/a ", "   "]) == (
        0,
        "content\n",
        "",
    )
    fetch_request = calls[-1]
    assert isinstance(fetch_request, URLFetchRequest)
    assert fetch_request.url == "https://example.com/a"
    assert fetch_request.focus is None

    response = ErrorResponse(ErrorCode.ALL_PROVIDERS_FAILED, "provider failure")
    assert await invoke(["keyword-search", "query"]) == (
        1,
        "",
        "provider failure\n",
    )

    protocol_failure = True
    assert await invoke(["keyword-search", "query"]) == (
        1,
        "",
        "bad local protocol\n",
    )
    protocol_failure = False

    response = SuccessResponse("Daemon stopped.")
    assert await invoke(["stop"]) == (0, "Daemon stopped.\n", "")
    shutdown_request = calls[-1]
    assert isinstance(shutdown_request, ShutdownRequest)

    daemon = _FakeDaemon()
    stdout = StringIO()
    stderr = StringIO()
    code = await run_command(
        parser.parse_args(["start"]),
        paths,
        client=client,
        daemon_factory=lambda _paths: daemon,
        stdout=stdout,
        stderr=stderr,
    )
    assert code == 0
    assert daemon.start_calls == 1
    assert stdout.getvalue() == ""
    assert stderr.getvalue() == ""
