"""Thin command-line client for the local foreground gateway daemon."""

import argparse
import asyncio
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol, TextIO

from .daemon import ForegroundDaemon
from .errors import DaemonUnavailable, ErrorCode, GatewayError, InputFailure
from .models import (
    ErrorResponse,
    KeywordSearchRequest,
    LLMSearchRequest,
    Request,
    Response,
    ShutdownRequest,
    SuccessResponse,
    URLFetchRequest,
)
from .paths import RuntimePaths
from .protocol import send_request
from .url_normalization import normalize_url

EXIT_OK = 0
EXIT_ERROR = 1
_START_INSTRUCTION = "Start the daemon with: agent-search-gateway start"

SocketClient = Callable[[Path, Request], Awaitable[Response]]


class DaemonLike(Protocol):
    async def start(self) -> None: ...


DaemonFactory = Callable[[RuntimePaths], DaemonLike]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-search-gateway")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("start")
    subparsers.add_parser("stop")

    keyword = subparsers.add_parser("keyword-search")
    keyword.add_argument("query")

    llm = subparsers.add_parser("llm-search")
    llm.add_argument("prompt")

    fetch = subparsers.add_parser("url-fetch")
    fetch.add_argument("url")
    fetch.add_argument("focus", nargs="?")
    return parser


def _request_from_args(args: argparse.Namespace) -> Request:
    if args.command == "stop":
        return ShutdownRequest()
    if args.command == "keyword-search":
        query = args.query.strip()
        if not query:
            raise InputFailure(ErrorCode.EMPTY_QUERY, "Query must not be empty")
        return KeywordSearchRequest(query)
    if args.command == "llm-search":
        prompt = args.prompt.strip()
        if not prompt:
            raise InputFailure(ErrorCode.EMPTY_QUERY, "Prompt must not be empty")
        return LLMSearchRequest(prompt)
    if args.command == "url-fetch":
        url = normalize_url(args.url)
        focus = args.focus.strip() if args.focus is not None and args.focus.strip() else None
        return URLFetchRequest(str(url), focus)
    raise InputFailure(ErrorCode.BAD_REQUEST, "Unknown command")


def _write_text(stream: TextIO, text: str) -> None:
    stream.write(text)
    if not text.endswith("\n"):
        stream.write("\n")


async def run_command(
    args: argparse.Namespace,
    paths: RuntimePaths,
    *,
    client: SocketClient = send_request,
    daemon_factory: DaemonFactory = ForegroundDaemon,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    if args.command == "start":
        try:
            daemon = daemon_factory(paths)
            await daemon.start()
        except GatewayError as exc:
            _write_text(stderr, exc.message)
            return EXIT_ERROR
        return EXIT_OK

    try:
        request = _request_from_args(args)
    except GatewayError as exc:
        _write_text(stderr, exc.message)
        return EXIT_ERROR

    try:
        response = await client(paths.socket_file, request)
    except DaemonUnavailable:
        if isinstance(request, ShutdownRequest):
            _write_text(stdout, "Daemon is not running.")
            return EXIT_OK
        _write_text(stderr, _START_INSTRUCTION)
        return EXIT_ERROR
    except GatewayError as exc:
        _write_text(stderr, exc.message)
        return EXIT_ERROR

    if isinstance(response, SuccessResponse):
        _write_text(stdout, response.text)
        return EXIT_OK
    if isinstance(response, ErrorResponse):
        _write_text(stderr, response.message)
        return EXIT_ERROR
    _write_text(stderr, "Invalid daemon response")
    return EXIT_ERROR


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return asyncio.run(
        run_command(
            args,
            RuntimePaths.default(),
            stdout=sys.stdout,
            stderr=sys.stderr,
        )
    )
