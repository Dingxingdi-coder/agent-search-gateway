"""Bounded, read-only inspection of the local daemon Unix socket."""

import asyncio
import stat
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

SOCKET_PROBE_TIMEOUT_SECONDS = 2.0
SocketConnector = Callable[..., Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]


class SocketState(StrEnum):
    MISSING = "missing"
    LIVE = "live"
    REFUSED = "refused"
    NOT_SOCKET = "not_socket"
    TIMEOUT = "timeout"
    OS_ERROR = "os_error"


@dataclass(frozen=True, slots=True)
class SocketProbeResult:
    state: SocketState
    identity: tuple[int, int] | None = None
    reason: str = ""


async def probe_unix_socket(
    path: Path,
    timeout_seconds: float = SOCKET_PROBE_TIMEOUT_SECONDS,
    connector: SocketConnector = asyncio.open_unix_connection,
) -> SocketProbeResult:
    try:
        existing = path.lstat()
    except FileNotFoundError:
        return SocketProbeResult(SocketState.MISSING)
    except OSError as exc:
        return SocketProbeResult(SocketState.OS_ERROR, reason=_safe_reason(exc))

    if not stat.S_ISSOCK(existing.st_mode):
        return SocketProbeResult(SocketState.NOT_SOCKET)
    identity = (existing.st_dev, existing.st_ino)

    try:
        _, writer = await asyncio.wait_for(
            connector(path=path),
            timeout=timeout_seconds,
        )
    except FileNotFoundError:
        return SocketProbeResult(SocketState.MISSING)
    except ConnectionRefusedError as exc:
        return SocketProbeResult(SocketState.REFUSED, identity, _safe_reason(exc))
    except TimeoutError as exc:
        return SocketProbeResult(SocketState.TIMEOUT, identity, _safe_reason(exc))
    except OSError as exc:
        return SocketProbeResult(SocketState.OS_ERROR, identity, _safe_reason(exc))

    writer.close()
    with suppress(ConnectionError, BrokenPipeError):
        await writer.wait_closed()
    return SocketProbeResult(SocketState.LIVE, identity)


def _safe_reason(exc: OSError | TimeoutError) -> str:
    text = str(exc).strip()
    return text if text else type(exc).__name__
