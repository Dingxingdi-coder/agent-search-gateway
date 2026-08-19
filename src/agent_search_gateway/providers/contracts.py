"""Provider-facing contracts and candidate value objects."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..models import LLMInvocation
from ..url_normalization import NormalizedURL

ChatMessage = Mapping[str, str]


@dataclass(frozen=True, slots=True)
class KeywordSearchHit:
    url: str
    title: str = ""
    snippet: str = ""
    raw_content: str = ""
    content: str = ""


@dataclass(frozen=True, slots=True)
class URLFetchCandidate:
    raw_content: str
    content: str = ""


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    search: bool
    fetch: bool


@runtime_checkable
class KeywordSearchProvider(Protocol):
    name: str

    async def search(self, query: str) -> list[KeywordSearchHit]: ...


@runtime_checkable
class URLFetchProvider(Protocol):
    name: str

    async def fetch(self, url: NormalizedURL) -> URLFetchCandidate: ...


@runtime_checkable
class LLMClient(Protocol):
    name: str

    async def complete_text(
        self,
        invocation: LLMInvocation,
        messages: Sequence[ChatMessage],
    ) -> str: ...

    async def complete_json(
        self,
        invocation: LLMInvocation,
        messages: Sequence[ChatMessage],
    ) -> Mapping[str, object]: ...

    async def aclose(self) -> None: ...
