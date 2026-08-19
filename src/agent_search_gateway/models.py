"""Side-effect-free domain and protocol data models."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, TypeAlias

from .errors import ErrorCode, ExecutionFailure
from .url_normalization import NormalizedURL

if TYPE_CHECKING:
    from .providers.contracts import URLFetchCandidate


@dataclass(frozen=True, slots=True)
class URLRecord:
    url: NormalizedURL
    raw_content: str = ""
    content: str = ""
    abstract: str = ""
    available: bool = True


@dataclass(frozen=True, slots=True)
class SearchRecord:
    url: NormalizedURL
    abstract: str


@dataclass(frozen=True, slots=True)
class LLMInvocation:
    provider: str
    model: str
    extra_body: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int
    base_delay_seconds: float
    max_delay_seconds: float
    request_timeout_seconds: float


@dataclass(frozen=True, slots=True)
class StageDecision:
    ok: bool
    reason: str = ""


@dataclass(frozen=True, slots=True)
class KeywordSearchRequest:
    query: str


@dataclass(frozen=True, slots=True)
class LLMSearchRequest:
    prompt: str


@dataclass(frozen=True, slots=True)
class URLFetchRequest:
    url: str
    focus: str | None = None


@dataclass(frozen=True, slots=True)
class ShutdownRequest:
    pass


Request: TypeAlias = KeywordSearchRequest | LLMSearchRequest | URLFetchRequest | ShutdownRequest


@dataclass(frozen=True, slots=True)
class SuccessResponse:
    text: str
    ok: bool = field(default=True, init=False)


@dataclass(frozen=True, slots=True)
class ErrorResponse:
    error: ErrorCode
    message: str
    ok: bool = field(default=False, init=False)


Response: TypeAlias = SuccessResponse | ErrorResponse


@dataclass(frozen=True, slots=True)
class FetchOutcome:
    kind: Literal["accepted", "semantic_failure", "execution_failure"]
    candidate: URLFetchCandidate | None = None
    failures: tuple[ExecutionFailure, ...] = ()
