"""Foreground Unix-socket daemon and graceful shutdown lifecycle."""

import asyncio
import logging
import os
import stat
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from pathlib import Path
from typing import Protocol

from .config import load_toml, resolve_config
from .errors import ConfigFailure, ErrorCode, GatewayError
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
from .protocol import NDJSONDecoder, encode_response
from .providers.defaults import build_default_registry
from .request_ids import RequestIdFactory, RequestIdRegistry, bind_request_id, generate_request_id
from .runtime import Runtime

_SHUTDOWN_GRACE_SECONDS = 10.0
_SOCKET_PROBE_TIMEOUT_SECONDS = 2.0


class _SearchOrchestratorLike(Protocol):
    async def keyword_search(self, query: str, *, request_id: str) -> str: ...

    async def llm_search(self, prompt: str, *, request_id: str) -> str: ...


class _FetchOrchestratorLike(Protocol):
    async def url_fetch(self, url: str, focus: str | None = None) -> str: ...


class RuntimeLike(Protocol):
    @property
    def search_orchestrator(self) -> _SearchOrchestratorLike: ...

    @property
    def fetch_orchestrator(self) -> _FetchOrchestratorLike: ...

    async def aclose(self) -> None: ...


RuntimeFactory = Callable[[], RuntimeLike]
ShutdownWaiter = Callable[[tuple[asyncio.Task[object], ...], float], Awaitable[None]]


async def _default_shutdown_waiter(
    tasks: tuple[asyncio.Task[object], ...],
    timeout: float,
) -> None:
    if not tasks:
        return
    _, pending = await asyncio.wait(tasks, timeout=timeout)
    if pending:
        raise TimeoutError


class ForegroundDaemon:
    def __init__(
        self,
        paths: RuntimePaths,
        *,
        runtime_factory: RuntimeFactory | None = None,
        environ: Mapping[str, str] | None = None,
        logger: logging.Logger | None = None,
        shutdown_waiter: ShutdownWaiter = _default_shutdown_waiter,
        request_id_factory: RequestIdFactory = generate_request_id,
    ) -> None:
        self.paths = paths
        self.ready = asyncio.Event()
        self.stopped = asyncio.Event()
        self._logger = logger or logging.getLogger(__name__)
        self._environ = dict(os.environ if environ is None else environ)
        self._runtime_factory = runtime_factory
        self._shutdown_waiter = shutdown_waiter
        self._request_ids = RequestIdRegistry(paths.results_dir, factory=request_id_factory)
        self._runtime: RuntimeLike | None = None
        self._server: asyncio.AbstractServer | None = None
        self._socket_identity: tuple[int, int] | None = None
        self._state_lock = asyncio.Lock()
        self._shutting_down = False
        self._shutdown_task: asyncio.Task[None] | None = None
        self._shutdown_response_ready = asyncio.Event()
        self._active_workflows: set[asyncio.Task[object]] = set()

    @property
    def shutting_down(self) -> bool:
        return self._shutting_down

    async def start(self) -> None:
        self.paths.socket_file.parent.mkdir(parents=True, exist_ok=True)
        self.paths.results_dir.mkdir(parents=True, exist_ok=True)
        try:
            await self._prepare_socket_path()
            self._runtime = self._create_runtime()
            try:
                self._server = await asyncio.start_unix_server(
                    self._handle_connection,
                    path=self.paths.socket_file,
                )
            except OSError as exc:
                await self._close_runtime_after_startup_failure()
                raise ConfigFailure(
                    ErrorCode.CONFIG_ERROR,
                    f"Failed to bind daemon socket: {self.paths.socket_file}",
                ) from exc
            socket_stat = self.paths.socket_file.stat()
            self._socket_identity = (socket_stat.st_dev, socket_stat.st_ino)
            self.ready.set()
            await self.stopped.wait()
        except (asyncio.CancelledError, KeyboardInterrupt):
            await self.stop_for_test()
            raise

    async def _prepare_socket_path(self) -> None:
        path = self.paths.socket_file
        try:
            existing = path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(existing.st_mode):
            return

        identity = (existing.st_dev, existing.st_ino)
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(path=path),
                timeout=_SOCKET_PROBE_TIMEOUT_SECONDS,
            )
        except FileNotFoundError:
            return
        except ConnectionRefusedError:
            pass
        except TimeoutError as exc:
            raise ConfigFailure(
                ErrorCode.CONFIG_ERROR,
                f"Daemon socket did not respond in time: {path}",
            ) from exc
        except OSError as exc:
            raise ConfigFailure(
                ErrorCode.CONFIG_ERROR,
                f"Unable to inspect daemon socket: {path}",
            ) from exc
        else:
            writer.close()
            with suppress(ConnectionError, BrokenPipeError):
                await writer.wait_closed()
            raise ConfigFailure(
                ErrorCode.CONFIG_ERROR,
                f"Daemon is already running at: {path}",
            )

        try:
            current = path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(current.st_mode) or (current.st_dev, current.st_ino) != identity:
            raise ConfigFailure(
                ErrorCode.CONFIG_ERROR,
                f"Daemon socket changed during startup: {path}",
            )
        try:
            path.unlink()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise ConfigFailure(
                ErrorCode.CONFIG_ERROR,
                f"Failed to remove stale daemon socket: {path}",
            ) from exc

    def _create_runtime(self) -> RuntimeLike:
        try:
            if self._runtime_factory is not None:
                return self._runtime_factory()
            registry = build_default_registry()
            data = load_toml(self.paths.config_file)
            config = resolve_config(data, registry, self._environ)
            return Runtime.build(config, self.paths, registry=registry)
        except ConfigFailure:
            raise
        except Exception as exc:
            raise ConfigFailure(
                ErrorCode.CONFIG_ERROR,
                "Failed to initialize daemon runtime",
            ) from exc

    async def _close_runtime_after_startup_failure(self) -> None:
        runtime = self._runtime
        self._runtime = None
        if runtime is None:
            return
        try:
            await runtime.aclose()
        except Exception:
            self._logger.error("daemon runtime close failed during startup cleanup")

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        decoder = NDJSONDecoder()
        try:
            while data := await reader.read(4096):
                for decoded in decoder.feed(data):
                    if isinstance(decoded, ErrorResponse):
                        response: Response = decoded
                    else:
                        response = await self._dispatch(decoded)
                    writer.write(encode_response(response))
                    await writer.drain()
        except (ConnectionError, BrokenPipeError):
            return
        finally:
            writer.close()
            with suppress(ConnectionError, BrokenPipeError):
                await writer.wait_closed()

    async def _dispatch(self, request: Request) -> Response:
        if isinstance(request, ShutdownRequest):
            await self._begin_shutdown()
            await self._shutdown_response_ready.wait()
            return SuccessResponse("Daemon stopped.")
        if not isinstance(request, (KeywordSearchRequest, LLMSearchRequest, URLFetchRequest)):
            return ErrorResponse(ErrorCode.BAD_REQUEST, "Unknown request type")

        current = asyncio.current_task()
        if current is None:
            return ErrorResponse(ErrorCode.PROTOCOL_ERROR, "Internal daemon error")
        workflow_task = current
        async with self._state_lock:
            if self._shutting_down:
                return ErrorResponse(
                    ErrorCode.DAEMON_SHUTTING_DOWN,
                    "Daemon is shutting down",
                )
            self._active_workflows.add(workflow_task)
        try:
            is_search = isinstance(request, (KeywordSearchRequest, LLMSearchRequest))
            with (
                self._request_ids.reserve(may_write_search_result=is_search) as request_id,
                bind_request_id(request_id),
            ):
                runtime = self._require_runtime()
                if isinstance(request, KeywordSearchRequest):
                    text = await runtime.search_orchestrator.keyword_search(
                        request.query,
                        request_id=request_id,
                    )
                elif isinstance(request, LLMSearchRequest):
                    text = await runtime.search_orchestrator.llm_search(
                        request.prompt,
                        request_id=request_id,
                    )
                else:
                    text = await runtime.fetch_orchestrator.url_fetch(
                        request.url,
                        request.focus,
                    )
            return SuccessResponse(text)
        except GatewayError as exc:
            return ErrorResponse(exc.code, exc.message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._logger.error(
                "unexpected daemon workflow failure type=%s",
                type(exc).__name__,
            )
            return ErrorResponse(ErrorCode.PROTOCOL_ERROR, "Internal daemon error")
        finally:
            async with self._state_lock:
                self._active_workflows.discard(workflow_task)

    def _require_runtime(self) -> RuntimeLike:
        if self._runtime is None:
            raise RuntimeError("daemon runtime is not initialized")
        return self._runtime

    async def _begin_shutdown(self) -> asyncio.Task[None]:
        async with self._state_lock:
            if self._shutdown_task is None:
                self._shutting_down = True
                self._shutdown_task = asyncio.create_task(self._shutdown_coordinator())
            return self._shutdown_task

    async def _shutdown_coordinator(self) -> None:
        server: asyncio.AbstractServer | None = None
        try:
            async with self._state_lock:
                active = tuple(self._active_workflows)
            try:
                await self._shutdown_waiter(active, _SHUTDOWN_GRACE_SECONDS)
            except TimeoutError:
                for task in active:
                    if not task.done():
                        task.cancel()
                if active:
                    await asyncio.gather(*active, return_exceptions=True)
            server = await self._cleanup_before_response()
            self._shutdown_response_ready.set()
            if server is not None:
                try:
                    await server.wait_closed()
                except Exception:
                    self._logger.error("daemon socket server close failed")
        finally:
            self._shutdown_response_ready.set()
            self.stopped.set()

    async def _cleanup_before_response(self) -> asyncio.AbstractServer | None:
        runtime = self._runtime
        self._runtime = None
        if runtime is not None:
            try:
                await runtime.aclose()
            except Exception:
                self._logger.error("daemon runtime close failed")

        server = self._server
        self._server = None
        if server is not None:
            try:
                server.close()
            except Exception:
                self._logger.error("daemon socket server close failed")

        if self._owns_socket(self.paths.socket_file):
            try:
                self.paths.socket_file.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                self._logger.error("daemon socket unlink failed")
        return server

    def _owns_socket(self, path: Path) -> bool:
        identity = self._socket_identity
        if identity is None:
            return False
        try:
            stat = path.stat()
        except FileNotFoundError:
            return False
        return (stat.st_dev, stat.st_ino) == identity

    async def stop_for_test(self) -> None:
        task = await self._begin_shutdown()
        await task
