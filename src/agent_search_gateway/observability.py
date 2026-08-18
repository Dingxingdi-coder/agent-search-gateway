"""Secret-safe observability primitives."""

import logging
from collections.abc import Iterable


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


class SecretRedactingFilter(logging.Filter):
    def __init__(self, secrets: Iterable[SecretValue]) -> None:
        super().__init__()
        self._secrets = tuple(secret.reveal() for secret in secrets if secret.reveal())

    def filter(self, record: logging.LogRecord) -> bool:
        rendered = record.getMessage()
        for secret in self._secrets:
            rendered = rendered.replace(secret, "<redacted>")
        record.msg = rendered
        record.args = ()
        return True
