"""Local, deterministic health diagnostics for the gateway CLI."""

import os
import secrets
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TextIO

from .config import load_toml, resolve_config
from .errors import ConfigFailure
from .observability import normalize_log_reason
from .paths import RuntimePaths
from .providers.academic.defaults import (
    build_default_academic_registry,
    build_default_oa_resolver_registry,
)
from .providers.defaults import build_default_registry
from .socket_probe import SocketProbeResult, SocketState, probe_unix_socket


class DoctorStatus(StrEnum):
    OK = "ok"
    INFO = "info"
    FAIL = "fail"


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    status: DoctorStatus
    message: str


@dataclass(frozen=True, slots=True)
class DoctorReport:
    checks: tuple[DoctorCheck, ...]

    @property
    def exit_code(self) -> int:
        return 1 if any(check.status is DoctorStatus.FAIL for check in self.checks) else 0


DirectoryProbe = Callable[[Path], DoctorCheck]
SocketProbe = Callable[[Path], Awaitable[SocketProbeResult]]


async def run_doctor(
    paths: RuntimePaths,
    *,
    environ: Mapping[str, str],
    directory_probe: DirectoryProbe = lambda path: probe_directory_writable(path),
    socket_probe: SocketProbe = probe_unix_socket,
) -> DoctorReport:
    checks: list[DoctorCheck] = []
    checks.extend(_configuration_checks(paths.config_file, environ))
    checks.append(directory_probe(paths.socket_file.parent))
    checks.append(directory_probe(paths.results_dir))
    checks.append(directory_probe(paths.logs_dir))
    checks.append(_socket_check(paths.socket_file, await socket_probe(paths.socket_file)))
    return DoctorReport(tuple(checks))


def render_doctor(report: DoctorReport, stream: TextIO) -> None:
    for check in report.checks:
        message = normalize_log_reason(check.message, max_chars=1000)
        stream.write(f"[{check.status.value}] {message}\n")


def probe_directory_writable(path: Path) -> DoctorCheck:
    candidate = path
    while True:
        try:
            candidate.lstat()
        except FileNotFoundError:
            parent = candidate.parent
            if parent == candidate:
                return DoctorCheck(
                    DoctorStatus.FAIL,
                    f"directory has no existing parent: {path}",
                )
            candidate = parent
            continue
        except OSError as exc:
            return DoctorCheck(
                DoctorStatus.FAIL,
                f"unable to inspect directory path: {path}: {_safe_os_reason(exc)}",
            )
        break

    if not candidate.is_dir():
        if candidate == path:
            return DoctorCheck(
                DoctorStatus.FAIL,
                f"expected directory but found non-directory: {path}",
            )
        return DoctorCheck(
            DoctorStatus.FAIL,
            f"directory cannot be created because parent is not a directory: {candidate}",
        )
    if candidate == path:
        return _probe_existing_directory(path, target=path)

    probed = _probe_existing_directory(candidate, target=path)
    if probed.status is DoctorStatus.OK:
        return DoctorCheck(DoctorStatus.OK, f"directory is creatable: {path}")
    return probed


def _configuration_checks(
    config_file: Path,
    environ: Mapping[str, str],
) -> tuple[DoctorCheck, ...]:
    if not config_file.exists():
        return (DoctorCheck(DoctorStatus.FAIL, f"config file not found: {config_file}"),)
    try:
        data = load_toml(config_file)
    except ConfigFailure as exc:
        return (DoctorCheck(DoctorStatus.FAIL, f"config parse failed: {exc.message}"),)
    try:
        config = resolve_config(
            data,
            build_default_registry(),
            environ,
            academic_registry=build_default_academic_registry(),
            oa_resolver_registry=build_default_oa_resolver_registry(),
        )
    except ConfigFailure as exc:
        return (DoctorCheck(DoctorStatus.FAIL, f"configuration invalid: {exc.message}"),)

    checks: list[DoctorCheck] = [DoctorCheck(DoctorStatus.OK, "configuration valid")]
    env_names = {
        provider.api_key_env
        for provider in config.web.providers
        if provider.api_key_env is not None
    }
    env_names.update(provider.api_key_env for provider in config.llm.providers)
    for provider in config.academic.providers:
        if provider.api_key_env is not None:
            env_names.add(provider.api_key_env)
        if provider.contact_email_env is not None:
            env_names.add(provider.contact_email_env)
    if config.oa_resolver is not None:
        if config.oa_resolver.api_key_env is not None:
            env_names.add(config.oa_resolver.api_key_env)
        if config.oa_resolver.contact_email_env is not None:
            env_names.add(config.oa_resolver.contact_email_env)
    checks.extend(
        DoctorCheck(DoctorStatus.OK, f"environment variable {name} is set")
        for name in sorted(env_names)
    )
    return tuple(checks)


def _probe_existing_directory(directory: Path, *, target: Path) -> DoctorCheck:
    probe_file = directory / f".agent-search-gateway-doctor-{secrets.token_hex(8)}"
    descriptor: int | None = None
    created = False
    try:
        descriptor = os.open(probe_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
        os.write(descriptor, b"probe")
        os.close(descriptor)
        descriptor = None
        probe_file.unlink()
        created = False
    except OSError as exc:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)
        if created:
            try:
                probe_file.unlink()
            except OSError as cleanup_exc:
                return DoctorCheck(
                    DoctorStatus.FAIL,
                    f"directory probe cleanup failed: {target}: {_safe_os_reason(cleanup_exc)}",
                )
        return DoctorCheck(
            DoctorStatus.FAIL,
            f"directory is not writable: {target}: {_safe_os_reason(exc)}",
        )
    return DoctorCheck(DoctorStatus.OK, f"directory writable: {target}")


def _socket_check(path: Path, result: SocketProbeResult) -> DoctorCheck:
    if result.state is SocketState.MISSING:
        return DoctorCheck(DoctorStatus.INFO, "daemon not running")
    if result.state is SocketState.LIVE:
        return DoctorCheck(DoctorStatus.OK, "daemon running")
    if result.state is SocketState.REFUSED:
        return DoctorCheck(
            DoctorStatus.FAIL,
            f"daemon socket is stale or refusing connections: {path}",
        )
    if result.state is SocketState.TIMEOUT:
        return DoctorCheck(
            DoctorStatus.FAIL,
            f"daemon socket did not respond in time: {path}",
        )
    if result.state is SocketState.NOT_SOCKET:
        return DoctorCheck(
            DoctorStatus.FAIL,
            f"daemon socket path is not a Unix socket: {path}",
        )
    reason = normalize_log_reason(result.reason or "OS error", max_chars=300)
    return DoctorCheck(DoctorStatus.FAIL, f"unable to inspect daemon socket: {reason}")


def _safe_os_reason(exc: OSError) -> str:
    return normalize_log_reason(str(exc) or type(exc).__name__, max_chars=300)
