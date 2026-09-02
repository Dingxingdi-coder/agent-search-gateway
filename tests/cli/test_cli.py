import os
from io import StringIO
from pathlib import Path
from typing import cast

import pytest

from agent_search_gateway.cli import build_parser, run_command
from agent_search_gateway.doctor import DoctorCheck, DoctorReport, DoctorStatus
from agent_search_gateway.errors import ConfigFailure, DaemonUnavailable, ErrorCode, ProtocolFailure
from agent_search_gateway.models import (
    ErrorResponse,
    KeywordSearchRequest,
    LLMSearchRequest,
    PaperSearchRequest,
    Request,
    Response,
    ShutdownRequest,
    SuccessResponse,
    URLFetchRequest,
)
from agent_search_gateway.observability import DebugLoggingSession
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
        ["doctor"],
        ["keyword-search", "query"],
        ["paper-search", "query"],
        ["llm-search", "prompt"],
        ["llm-search", "prompt", "--scope", "paper"],
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
            daemon_factory=lambda _paths, **_kwargs: _FakeDaemon(),
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
    assert await invoke(["paper-search", "   "]) == (
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

    response = SuccessResponse("/tmp/papers.jsonl")
    assert await invoke(["paper-search", "  papers  "]) == (
        0,
        "/tmp/papers.jsonl\n",
        "",
    )
    paper_request = calls[-1]
    assert isinstance(paper_request, PaperSearchRequest)
    assert paper_request.query == "papers"

    response = SuccessResponse("llm result")
    assert await invoke(["llm-search", "  prompt  "]) == (0, "llm result\n", "")
    llm_request = calls[-1]
    assert isinstance(llm_request, LLMSearchRequest)
    assert llm_request.prompt == "prompt"
    assert llm_request.scope == "web"

    response = SuccessResponse("paper llm result")
    assert await invoke(["llm-search", "prompt", "--scope", "paper"]) == (
        0,
        "paper llm result\n",
        "",
    )
    scoped_request = calls[-1]
    assert isinstance(scoped_request, LLMSearchRequest)
    assert scoped_request == LLMSearchRequest("prompt", "paper")

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
        daemon_factory=lambda _paths, **_kwargs: daemon,
        stdout=stdout,
        stderr=stderr,
    )
    assert code == 0
    assert daemon.start_calls == 1
    assert stdout.getvalue() == ""
    assert stderr.getvalue() == ""


def test_start_debug_is_scoped_to_start_parser() -> None:
    parser = build_parser()

    assert parser.parse_args(["start"]).debug is False
    assert parser.parse_args(["start", "--debug"]).debug is True
    for argv in (
        ["stop", "--debug"],
        ["doctor", "--debug"],
        ["keyword-search", "query", "--debug"],
        ["llm-search", "prompt", "--debug"],
        ["url-fetch", "https://example.com", "--debug"],
    ):
        with pytest.raises(SystemExit):
            parser.parse_args(argv)


async def test_start_debug_configures_before_factory_and_always_closes(tmp_path: Path) -> None:
    parser = build_parser()
    paths = RuntimePaths.from_home(tmp_path)
    events: list[str] = []

    class FakeSession:
        def close(self) -> None:
            events.append("close")

    session = cast(DebugLoggingSession, FakeSession())
    daemon = _FakeDaemon()

    def configure(log_file: Path, *, stderr: StringIO) -> DebugLoggingSession:
        assert log_file == paths.debug_log_file
        events.append("configure")
        return session

    def factory(
        received_paths: RuntimePaths,
        *,
        debug: bool,
        logging_session: DebugLoggingSession | None,
    ) -> _FakeDaemon:
        assert received_paths == paths
        assert debug is True
        assert logging_session is session
        events.append("factory")
        return daemon

    stdout = StringIO()
    stderr = StringIO()
    code = await run_command(
        parser.parse_args(["start", "--debug"]),
        paths,
        daemon_factory=factory,
        logging_configurer=configure,
        stdout=stdout,
        stderr=stderr,
    )

    assert code == 0
    assert daemon.start_calls == 1
    assert events == ["configure", "factory", "close"]
    assert stdout.getvalue() == ""
    assert stderr.getvalue() == ""


async def test_start_debug_bootstrap_failure_is_safe_and_normal_start_is_unaffected(
    tmp_path: Path,
) -> None:
    parser = build_parser()
    paths = RuntimePaths.from_home(tmp_path)
    factory_calls = 0
    configure_calls = 0

    def failing_configurer(log_file: Path, *, stderr: StringIO) -> DebugLoggingSession:
        nonlocal configure_calls
        configure_calls += 1
        raise ConfigFailure(ErrorCode.CONFIG_ERROR, "safe debug bootstrap failure")

    def factory(
        received_paths: RuntimePaths,
        *,
        debug: bool,
        logging_session: DebugLoggingSession | None,
    ) -> _FakeDaemon:
        nonlocal factory_calls
        factory_calls += 1
        return _FakeDaemon()

    debug_stdout = StringIO()
    debug_stderr = StringIO()
    debug_code = await run_command(
        parser.parse_args(["start", "--debug"]),
        paths,
        daemon_factory=factory,
        logging_configurer=failing_configurer,
        stdout=debug_stdout,
        stderr=debug_stderr,
    )
    assert debug_code == 1
    assert factory_calls == 0
    assert configure_calls == 1
    assert debug_stdout.getvalue() == ""
    assert debug_stderr.getvalue() == "safe debug bootstrap failure\n"

    normal_stdout = StringIO()
    normal_stderr = StringIO()
    normal_code = await run_command(
        parser.parse_args(["start"]),
        paths,
        daemon_factory=factory,
        logging_configurer=failing_configurer,
        stdout=normal_stdout,
        stderr=normal_stderr,
    )
    assert normal_code == 0
    assert factory_calls == 1
    assert configure_calls == 1
    assert not paths.debug_log_file.exists()


async def test_doctor_runs_locally_renders_all_checks_and_uses_report_exit_code(
    tmp_path: Path,
) -> None:
    parser = build_parser()
    paths = RuntimePaths.from_home(tmp_path)
    expected_environ = {"DOCTOR_ENV_NAME": "present"}
    report = DoctorReport(
        (
            DoctorCheck(DoctorStatus.OK, "configuration valid"),
            DoctorCheck(DoctorStatus.INFO, "daemon not running"),
            DoctorCheck(DoctorStatus.FAIL, "synthetic failure"),
        )
    )
    runner_calls = 0

    async def doctor_runner(
        received_paths: RuntimePaths,
        *,
        environ: dict[str, str],
    ) -> DoctorReport:
        nonlocal runner_calls
        runner_calls += 1
        assert received_paths == paths
        assert environ == expected_environ
        return report

    async def forbidden_client(_socket_path: Path, _request: Request) -> Response:
        raise AssertionError("doctor must not use socket client")

    def forbidden_daemon_factory(*args: object, **kwargs: object) -> _FakeDaemon:
        raise AssertionError("doctor must not construct daemon")

    stdout = StringIO()
    stderr = StringIO()
    code = await run_command(
        parser.parse_args(["doctor"]),
        paths,
        client=forbidden_client,
        daemon_factory=forbidden_daemon_factory,
        doctor_runner=doctor_runner,
        environ=expected_environ,
        stdout=stdout,
        stderr=stderr,
    )

    assert runner_calls == 1
    assert code == 1
    assert stdout.getvalue() == (
        "[ok] configuration valid\n[info] daemon not running\n[fail] synthetic failure\n"
    )
    assert stderr.getvalue() == ""


async def test_doctor_uses_process_environ_by_default_without_mutation(tmp_path: Path) -> None:
    parser = build_parser()
    paths = RuntimePaths.from_home(tmp_path)
    original_environ = dict(os.environ)
    recorded_environ: dict[str, str] | None = None

    async def recording_runner(
        received_paths: RuntimePaths,
        *,
        environ: dict[str, str],
    ) -> DoctorReport:
        nonlocal recorded_environ
        assert received_paths == paths
        recorded_environ = dict(environ)
        return DoctorReport((DoctorCheck(DoctorStatus.OK, "configuration valid"),))

    stdout = StringIO()
    stderr = StringIO()
    code = await run_command(
        parser.parse_args(["doctor"]),
        paths,
        doctor_runner=recording_runner,
        stdout=stdout,
        stderr=stderr,
    )

    assert code == 0
    assert recorded_environ == original_environ
    assert dict(os.environ) == original_environ
    assert stdout.getvalue() == "[ok] configuration valid\n"
    assert stderr.getvalue() == ""


async def test_doctor_unexpected_internal_failure_is_concise(tmp_path: Path) -> None:
    parser = build_parser()
    paths = RuntimePaths.from_home(tmp_path)

    async def broken_runner(
        received_paths: RuntimePaths,
        *,
        environ: dict[str, str],
    ) -> DoctorReport:
        raise RuntimeError("sensitive internal detail")

    stdout = StringIO()
    stderr = StringIO()
    code = await run_command(
        parser.parse_args(["doctor"]),
        paths,
        doctor_runner=broken_runner,
        environ={},
        stdout=stdout,
        stderr=stderr,
    )

    assert code == 1
    assert stdout.getvalue() == ""
    assert stderr.getvalue() == "[fail] doctor internal error\n"
    assert "sensitive internal detail" not in stderr.getvalue()
