"""Secret-safe structured observability primitives."""

import json
import logging
import re
from collections.abc import Callable, Iterable
from contextlib import suppress
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TextIO

from .errors import ConfigFailure, ErrorCode
from .request_ids import current_request_id

LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3

FileHandlerFactory = Callable[..., logging.Handler]

_FIELD_NAME = re.compile(r"[a-z][a-z0-9_]*\Z")
_PREFERRED_FIELD_ORDER = ("provider", "stage", "url", "event")
_EMERGENCY_MESSAGE = "agent-search-gateway debug logging sink failure\n"


class SecretValue:
    """A secret whose implicit string representations are always redacted."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "<redacted>"

    def __str__(self) -> str:
        return "<redacted>"


class SecretRedactor:
    """Redact configured secrets from fully rendered logging output."""

    def __init__(self, secrets: Iterable[SecretValue] = ()) -> None:
        self._secrets: set[str] = set()
        self.add_secrets(secrets)

    def add_secrets(self, secrets: Iterable[SecretValue]) -> None:
        for secret in secrets:
            value = secret.reveal()
            if not value:
                continue
            self._secrets.add(value)
            escaped = json.dumps(value, ensure_ascii=False)[1:-1]
            if escaped:
                self._secrets.add(escaped)

    def redact(self, rendered: str) -> str:
        for secret in sorted(self._secrets, key=len, reverse=True):
            rendered = rendered.replace(secret, "<redacted>")
        return rendered


class SecretRedactingFilter(logging.Filter):
    """Compatibility filter for existing provider tests and ad-hoc handlers."""

    def __init__(self, secrets: Iterable[SecretValue]) -> None:
        super().__init__()
        self._redactor = SecretRedactor(secrets)

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._redactor.redact(record.getMessage())
        record.args = ()
        return True


def normalize_log_reason(value: str, max_chars: int = 160) -> str:
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    normalized = " ".join(value.split())
    if len(normalized) <= max_chars:
        return normalized
    if max_chars == 1:
        return "…"
    return f"{normalized[: max_chars - 1]}…"


def _render_string(value: str) -> str:
    if value and value.isprintable() and not any(
        character.isspace() or character in {'"', "\\"} for character in value
    ):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _render_scalar(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return _render_string(value)
    if isinstance(value, (int, float)):
        return repr(value)
    return _render_string(f"<{type(value).__name__}>")


def _validated_field_name(name: str) -> str:
    if _FIELD_NAME.fullmatch(name) is None:
        raise ValueError(f"invalid log field name: {name}")
    return name


class KeyValueFormatter(logging.Formatter):
    """Render structured events as deterministic, single physical lines."""

    def __init__(self, redactor: SecretRedactor) -> None:
        super().__init__()
        self._redactor = redactor

    def format(self, record: logging.LogRecord) -> str:
        event = getattr(record, "gateway_event", None)
        raw_fields = getattr(record, "gateway_fields", None)
        if isinstance(event, str) and isinstance(raw_fields, dict):
            fields = dict(raw_fields)
        else:
            event = "log"
            fields = {"message": record.getMessage()}

        values: dict[str, object] = {
            _validated_field_name(str(key)): value for key, value in fields.items()
        }
        values["event"] = event
        request_id = current_request_id() or "-"
        parts = [record.levelname, f"request={request_id}"]
        emitted: set[str] = set()
        for name in _PREFERRED_FIELD_ORDER:
            if name not in values:
                continue
            parts.append(f"{name}={_render_scalar(values[name])}")
            emitted.add(name)
        for name in sorted(values.keys() - emitted):
            parts.append(f"{name}={_render_scalar(values[name])}")

        if record.exc_info:
            traceback_text = self.formatException(record.exc_info)
            parts.append(f"traceback={_render_string(traceback_text)}")
        return self._redactor.redact(" ".join(parts))


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    *,
    exc_info: bool | BaseException = False,
    **fields: object,
) -> None:
    _validated_field_name(event)
    for name in fields:
        _validated_field_name(name)
    logger.log(
        level,
        "",
        exc_info=exc_info,
        extra={"gateway_event": event, "gateway_fields": dict(fields)},
    )


def _write_emergency(stderr: TextIO) -> None:
    try:
        stderr.write(_EMERGENCY_MESSAGE)
        stderr.flush()
    except Exception:
        pass


class SafeRotatingFileHandler(RotatingFileHandler):
    """Rotating handler whose sink errors never escape into business code."""

    def __init__(
        self,
        filename: Path,
        *,
        stderr: TextIO,
        max_bytes: int,
        backup_count: int,
    ) -> None:
        self._emergency_stderr = stderr
        super().__init__(
            filename,
            mode="a",
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802
        _write_emergency(self._emergency_stderr)


class _SafeDelegatingHandler(logging.Handler):
    """Contain exceptions from injected handlers used by failure-path tests."""

    def __init__(self, delegate: logging.Handler, stderr: TextIO) -> None:
        super().__init__(delegate.level)
        self._delegate = delegate
        self._stderr = stderr

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._delegate.handle(record)
        except Exception:
            _write_emergency(self._stderr)

    def close(self) -> None:
        with suppress(Exception):
            self._delegate.close()
        super().close()


class DebugLoggingSession:
    """Own one debug logging configuration and restore prior logger state."""

    def __init__(
        self,
        logger: logging.Logger,
        handlers: tuple[logging.Handler, ...],
        redactor: SecretRedactor,
        *,
        previous_level: int,
        previous_propagate: bool,
    ) -> None:
        self._logger = logger
        self._handlers = handlers
        self._redactor = redactor
        self._previous_level = previous_level
        self._previous_propagate = previous_propagate
        self._closed = False

    def add_secrets(self, *secrets: SecretValue | Iterable[SecretValue]) -> None:
        collected: list[SecretValue] = []
        for item in secrets:
            if isinstance(item, SecretValue):
                collected.append(item)
            else:
                collected.extend(item)
        self._redactor.add_secrets(collected)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for handler in self._handlers:
            self._logger.removeHandler(handler)
            with suppress(Exception):
                handler.flush()
            with suppress(Exception):
                handler.close()
        self._logger.setLevel(self._previous_level)
        self._logger.propagate = self._previous_propagate


def _default_file_handler(
    log_file: Path,
    *,
    stderr: TextIO,
    max_bytes: int,
    backup_count: int,
) -> logging.Handler:
    return SafeRotatingFileHandler(
        log_file,
        stderr=stderr,
        max_bytes=max_bytes,
        backup_count=backup_count,
    )


def configure_debug_logging(
    log_file: Path,
    *,
    stderr: TextIO,
    max_bytes: int = LOG_MAX_BYTES,
    backup_count: int = LOG_BACKUP_COUNT,
    file_handler_factory: FileHandlerFactory | None = None,
) -> DebugLoggingSession:
    """Install project-only DEBUG handlers with rotation and final redaction."""

    logger = logging.getLogger("agent_search_gateway")
    previous_level = logger.level
    previous_propagate = logger.propagate
    handlers: list[logging.Handler] = []
    try:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        if backup_count < 0:
            raise ValueError("backup_count must not be negative")
        log_file.parent.mkdir(parents=True, exist_ok=True)
        redactor = SecretRedactor()
        formatter = KeyValueFormatter(redactor)

        stderr_handler = logging.StreamHandler(stderr)
        stderr_handler.setFormatter(formatter)
        handlers.append(stderr_handler)

        factory = file_handler_factory or _default_file_handler
        file_handler = factory(
            log_file,
            stderr=stderr,
            max_bytes=max_bytes,
            backup_count=backup_count,
        )
        if not isinstance(file_handler, logging.Handler):
            raise TypeError("file handler factory must return logging.Handler")
        file_handler.setFormatter(formatter)
        if file_handler_factory is not None:
            file_handler = _SafeDelegatingHandler(file_handler, stderr)
        handlers.append(file_handler)

        for handler in handlers:
            handler.__dict__["_agent_search_gateway_debug_owned"] = True
            logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        return DebugLoggingSession(
            logger,
            tuple(handlers),
            redactor,
            previous_level=previous_level,
            previous_propagate=previous_propagate,
        )
    except Exception as exc:
        for handler in handlers:
            logger.removeHandler(handler)
            with suppress(Exception):
                handler.close()
        logger.setLevel(previous_level)
        logger.propagate = previous_propagate
        raise ConfigFailure(ErrorCode.CONFIG_ERROR, "Failed to configure debug logging") from exc
