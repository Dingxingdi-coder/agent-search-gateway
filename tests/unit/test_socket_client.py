import asyncio
from pathlib import Path

import pytest

from agent_search_gateway.errors import DaemonUnavailable, ErrorCode, ProtocolFailure
from agent_search_gateway.models import KeywordSearchRequest, SuccessResponse
from agent_search_gateway.protocol import send_request


async def _run_server(path: Path, response: bytes, received: list[bytes]) -> asyncio.AbstractServer:
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        received.append(await reader.readline())
        writer.write(response)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    return await asyncio.start_unix_server(handler, path=path)


async def test_socket_client_sends_one_line_and_accepts_first_complete_response(
    tmp_path: Path,
) -> None:
    socket_path = tmp_path / "daemon.sock"
    received: list[bytes] = []
    server = await _run_server(
        socket_path,
        b'{"ok":true,"text":"done"}\n{"ok":true,"text":"ignored"}\n',
        received,
    )
    try:
        response = await send_request(socket_path, KeywordSearchRequest("hello"))
    finally:
        server.close()
        await server.wait_closed()

    assert response == SuccessResponse("done")
    assert received == [b'{"type":"keyword_search","query":"hello"}\n']


@pytest.mark.parametrize(
    "response",
    [
        b"not-json\n",
        b'{"ok":true,"text":"missing-newline"}',
        b'{"ok":true}\n',
        b'{"ok":"true","text":"wrong"}\n',
        b'{"ok":false,"error":"bad_request"}\n',
    ],
)
async def test_socket_client_rejects_malformed_response(tmp_path: Path, response: bytes) -> None:
    socket_path = tmp_path / "daemon.sock"
    received: list[bytes] = []
    server = await _run_server(socket_path, response, received)
    try:
        with pytest.raises(ProtocolFailure) as caught:
            await send_request(socket_path, KeywordSearchRequest("hello"))
    finally:
        server.close()
        await server.wait_closed()

    assert caught.value.code is ErrorCode.PROTOCOL_ERROR


async def test_socket_client_distinguishes_missing_daemon(tmp_path: Path) -> None:
    with pytest.raises(DaemonUnavailable):
        await send_request(tmp_path / "missing.sock", KeywordSearchRequest("hello"))
