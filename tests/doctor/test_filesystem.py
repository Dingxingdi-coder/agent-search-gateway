from pathlib import Path

from agent_search_gateway.doctor import (
    DoctorCheck,
    DoctorStatus,
    probe_directory_writable,
    run_doctor,
)
from agent_search_gateway.paths import RuntimePaths
from agent_search_gateway.socket_probe import SocketProbeResult, SocketState


async def _missing_socket(_path: Path) -> SocketProbeResult:
    return SocketProbeResult(SocketState.MISSING)


def test_directory_probe_is_transient_for_existing_and_missing_directories(tmp_path: Path) -> None:
    existing = tmp_path / "existing"
    existing.mkdir()
    before = set(existing.iterdir())

    existing_check = probe_directory_writable(existing)
    assert existing_check.status is DoctorStatus.OK
    assert set(existing.iterdir()) == before

    missing = tmp_path / "missing" / "nested"
    missing_check = probe_directory_writable(missing)
    assert missing_check.status is DoctorStatus.OK
    assert not missing.exists()
    assert not missing.parent.exists()


def test_directory_probe_rejects_non_directory_without_mutation(tmp_path: Path) -> None:
    occupied = tmp_path / "occupied"
    occupied.write_text("unchanged", encoding="utf-8")

    check = probe_directory_writable(occupied)

    assert check.status is DoctorStatus.FAIL
    assert "non-directory" in check.message
    assert occupied.read_text(encoding="utf-8") == "unchanged"


async def test_doctor_aggregates_directory_failures_and_leaves_no_runtime_artifacts(
    tmp_path: Path,
) -> None:
    paths = RuntimePaths.from_home(tmp_path)
    inspected: list[Path] = []

    def probe(path: Path) -> DoctorCheck:
        inspected.append(path)
        if path == paths.results_dir:
            return DoctorCheck(DoctorStatus.FAIL, f"synthetic directory failure: {path}")
        return DoctorCheck(DoctorStatus.OK, f"synthetic writable: {path}")

    report = await run_doctor(
        paths,
        environ={},
        directory_probe=probe,
        socket_probe=_missing_socket,
    )

    assert inspected == [paths.socket_file.parent, paths.results_dir, paths.logs_dir]
    assert report.exit_code == 1
    assert any("synthetic directory failure" in check.message for check in report.checks)
    assert not paths.results_dir.exists()
    assert not paths.logs_dir.exists()
    assert not paths.debug_log_file.exists()
    assert not paths.socket_file.exists()
