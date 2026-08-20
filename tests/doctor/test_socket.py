import asyncio
import socket
from pathlib import Path

from agent_search_gateway.doctor import DoctorCheck, DoctorStatus, run_doctor
from agent_search_gateway.paths import RuntimePaths
from agent_search_gateway.socket_probe import SocketProbeResult, SocketState


def _ok_directory(path: Path) -> DoctorCheck:
    return DoctorCheck(DoctorStatus.OK, f"ok: {path}")


async def test_doctor_socket_missing_is_informational(tmp_path: Path) -> None:
    paths = RuntimePaths.from_home(tmp_path)
    report = await run_doctor(paths, environ={}, directory_probe=_ok_directory)

    socket_check = report.checks[-1]
    assert socket_check.status is DoctorStatus.INFO
    assert socket_check.message == "daemon not running"


async def test_doctor_socket_live_is_ok_and_sends_no_business_frame(tmp_path: Path) -> None:
    paths = RuntimePaths.from_home(tmp_path)
    paths.socket_file.parent.mkdir(parents=True, exist_ok=True)
    received: list[bytes] = []
    handled = asyncio.Event()

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        received.append(await reader.read())
        writer.close()
        await writer.wait_closed()
        handled.set()

    server = await asyncio.start_unix_server(handler, path=paths.socket_file)
    try:
        report = await run_doctor(paths, environ={}, directory_probe=_ok_directory)
        await asyncio.wait_for(handled.wait(), timeout=1.0)
        socket_check = report.checks[-1]
        assert socket_check.status is DoctorStatus.OK
        assert socket_check.message == "daemon running"
        assert received == [b""]
    finally:
        server.close()
        await server.wait_closed()
        paths.socket_file.unlink(missing_ok=True)


async def test_doctor_socket_stale_regular_timeout_and_os_error_are_failures(
    tmp_path: Path,
) -> None:
    paths = RuntimePaths.from_home(tmp_path)
    paths.socket_file.parent.mkdir(parents=True, exist_ok=True)

    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(paths.socket_file))
    stale.close()
    stale_report = await run_doctor(paths, environ={}, directory_probe=_ok_directory)
    assert stale_report.checks[-1].status is DoctorStatus.FAIL
    assert "stale or refusing" in stale_report.checks[-1].message
    assert paths.socket_file.exists()
    paths.socket_file.unlink()

    paths.socket_file.write_text("not a socket", encoding="utf-8")
    regular_report = await run_doctor(paths, environ={}, directory_probe=_ok_directory)
    assert regular_report.checks[-1].status is DoctorStatus.FAIL
    assert "not a Unix socket" in regular_report.checks[-1].message
    paths.socket_file.unlink()

    async def timeout_probe(_path: Path) -> SocketProbeResult:
        return SocketProbeResult(SocketState.TIMEOUT, (1, 2), "timeout")

    timeout_report = await run_doctor(
        paths,
        environ={},
        directory_probe=_ok_directory,
        socket_probe=timeout_probe,
    )
    assert timeout_report.checks[-1].status is DoctorStatus.FAIL
    assert "did not respond in time" in timeout_report.checks[-1].message

    async def os_error_probe(_path: Path) -> SocketProbeResult:
        return SocketProbeResult(SocketState.OS_ERROR, (1, 2), "denied\nsecond line")

    os_error_report = await run_doctor(
        paths,
        environ={},
        directory_probe=_ok_directory,
        socket_probe=os_error_probe,
    )
    assert os_error_report.checks[-1].status is DoctorStatus.FAIL
    assert "denied second line" in os_error_report.checks[-1].message
