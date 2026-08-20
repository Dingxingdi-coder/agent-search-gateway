import asyncio
import socket
from pathlib import Path

from agent_search_gateway.socket_probe import SocketState, probe_unix_socket


async def test_socket_probe_classifies_missing_not_socket_and_refused(tmp_path: Path) -> None:
    missing = await probe_unix_socket(tmp_path / "missing.sock")
    assert missing.state is SocketState.MISSING
    assert missing.identity is None

    regular = tmp_path / "regular"
    regular.write_text("x", encoding="utf-8")
    not_socket = await probe_unix_socket(regular)
    assert not_socket.state is SocketState.NOT_SOCKET

    stale_path = tmp_path / "stale.sock"
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(stale_path))
    stale.close()
    refused = await probe_unix_socket(stale_path)
    assert refused.state is SocketState.REFUSED
    assert refused.identity is not None
    assert stale_path.exists()


async def test_socket_probe_live_connection_sends_no_bytes(tmp_path: Path) -> None:
    socket_path = tmp_path / "live.sock"
    received: list[bytes] = []
    handled = asyncio.Event()

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        received.append(await reader.read())
        writer.close()
        await writer.wait_closed()
        handled.set()

    server = await asyncio.start_unix_server(handler, path=socket_path)
    try:
        result = await probe_unix_socket(socket_path)
        assert result.state is SocketState.LIVE
        assert result.identity is not None
        await asyncio.wait_for(handled.wait(), timeout=1.0)
        assert received == [b""]
    finally:
        server.close()
        await server.wait_closed()
        socket_path.unlink(missing_ok=True)


async def test_socket_probe_timeout_and_os_error_are_bounded(tmp_path: Path) -> None:
    socket_path = tmp_path / "probe.sock"
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(socket_path))
    stale.listen(1)

    async def blocked_connector(
        **kwargs: object,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    try:
        timeout = await probe_unix_socket(
            socket_path,
            timeout_seconds=0.01,
            connector=blocked_connector,
        )
        assert timeout.state is SocketState.TIMEOUT

        async def failing_connector(
            **kwargs: object,
        ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
            raise PermissionError("denied")

        failed = await probe_unix_socket(socket_path, connector=failing_connector)
        assert failed.state is SocketState.OS_ERROR
        assert "denied" in failed.reason
    finally:
        stale.close()
        socket_path.unlink(missing_ok=True)
