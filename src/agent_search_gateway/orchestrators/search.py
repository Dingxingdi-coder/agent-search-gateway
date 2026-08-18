"""Keyword and LLM search workflow orchestration."""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass

from ..concurrency import ProviderQuotaManager
from ..errors import ErrorCode, ExecutionFailure, InputFailure
from ..llm.stages import LLMStages, cheap_check
from ..llm_search_parser import parse_search_markdown
from ..models import LLMInvocation, SearchRecord
from ..providers.contracts import KeywordSearchHit, KeywordSearchProvider
from ..result_writer import ResultWriter
from ..url_normalization import NormalizedURL, normalize_url
from ..url_store import URLStore


@dataclass(frozen=True, slots=True)
class _StagedKeyword:
    url: NormalizedURL
    abstract: str
    raw_content: str = ""
    content: str = ""


class SearchOrchestrator:
    def __init__(
        self,
        *,
        keyword_providers: Sequence[KeywordSearchProvider],
        llm_invocations: Sequence[LLMInvocation],
        quotas: ProviderQuotaManager,
        stages: LLMStages,
        store: URLStore,
        result_writer: ResultWriter,
    ) -> None:
        self._keyword_providers = tuple(keyword_providers)
        self._llm_invocations = tuple(llm_invocations)
        self.quotas = quotas
        self._stages = stages
        self._store = store
        self._result_writer = result_writer

    async def keyword_search(self, query: str) -> str:
        normalized_query = query.strip()
        if not normalized_query:
            raise InputFailure(ErrorCode.EMPTY_QUERY, "Query must not be empty")
        if not self._keyword_providers:
            raise ExecutionFailure(
                ErrorCode.NO_KEYWORD_SEARCH_PROVIDERS,
                "No keyword search providers are enabled",
            )

        outcomes = await asyncio.gather(
            *(
                self._run_keyword_pipeline(provider, normalized_query)
                for provider in self._keyword_providers
            ),
            return_exceptions=True,
        )
        completed = [outcome for outcome in outcomes if isinstance(outcome, list)]
        if not completed:
            raise ExecutionFailure(
                ErrorCode.ALL_PROVIDERS_FAILED,
                "All keyword search provider pipelines failed",
            )

        ordered_urls: list[NormalizedURL] = []
        seen: set[NormalizedURL] = set()
        for outcome in outcomes:
            if not isinstance(outcome, list):
                continue
            for staged in outcome:
                self._store.admit(
                    staged.url,
                    staged.abstract,
                    raw_content=staged.raw_content,
                    content=staged.content,
                )
                if staged.url not in seen:
                    seen.add(staged.url)
                    ordered_urls.append(staged.url)

        records = [self._record_from_store(url) for url in ordered_urls]
        return str(self._result_writer.write_results("keyword", records))

    async def llm_search(self, prompt: str) -> str:
        normalized_prompt = prompt.strip()
        if not normalized_prompt:
            raise InputFailure(ErrorCode.EMPTY_QUERY, "Prompt must not be empty")
        if not self._llm_invocations:
            raise ExecutionFailure(
                ErrorCode.NO_LLM_SEARCH_PROVIDERS,
                "No LLM search providers are configured",
            )

        outcomes = await asyncio.gather(
            *(
                self._run_llm_pipeline(invocation, normalized_prompt)
                for invocation in self._llm_invocations
            ),
            return_exceptions=True,
        )
        if not any(isinstance(outcome, list) for outcome in outcomes):
            raise ExecutionFailure(
                ErrorCode.ALL_PROVIDERS_FAILED,
                "All LLM search provider pipelines failed",
            )

        ordered_urls: list[NormalizedURL] = []
        seen: set[NormalizedURL] = set()
        for outcome in outcomes:
            if not isinstance(outcome, list):
                continue
            for result in outcome:
                self._store.admit(result.url, result.abstract)
                if result.url not in seen:
                    seen.add(result.url)
                    ordered_urls.append(result.url)
        records = [self._record_from_store(url) for url in ordered_urls]
        return str(self._result_writer.write_results("llm", records))

    async def _run_llm_pipeline(
        self,
        invocation: LLMInvocation,
        prompt: str,
    ) -> list[SearchRecord]:
        try:
            markdown = await self._stages.llm_search_markdown(invocation, prompt)
            return parse_search_markdown(markdown)
        except asyncio.CancelledError:
            raise
        except ExecutionFailure:
            raise
        except Exception as exc:
            raise ExecutionFailure(
                ErrorCode.ALL_PROVIDERS_FAILED,
                f"LLM search provider {invocation.provider} returned invalid data",
            ) from exc

    async def _run_keyword_pipeline(
        self,
        provider: KeywordSearchProvider,
        query: str,
    ) -> list[_StagedKeyword]:
        try:
            async with self.quotas.get_web(provider.name).lease():
                hits = await provider.search(query)
            if not isinstance(hits, list):
                raise TypeError("provider search result must be a list")
            staged: list[_StagedKeyword] = []
            for hit in hits:
                staged_hit = await self._stage_keyword_hit(hit)
                if staged_hit is not None:
                    staged.append(staged_hit)
            return staged
        except asyncio.CancelledError:
            raise
        except ExecutionFailure:
            raise
        except Exception as exc:
            raise ExecutionFailure(
                ErrorCode.ALL_PROVIDERS_FAILED,
                f"Keyword provider {provider.name} returned invalid data",
            ) from exc

    async def _stage_keyword_hit(self, hit: KeywordSearchHit) -> _StagedKeyword | None:
        if not isinstance(hit, KeywordSearchHit):
            raise TypeError("keyword hit has invalid type")
        for value in (hit.url, hit.title, hit.snippet, hit.raw_content, hit.content):
            if not isinstance(value, str):
                raise TypeError("keyword hit fields must be strings")

        abstract = hit.snippet.strip() or hit.title.strip()
        if not abstract:
            return None
        url = normalize_url(hit.url)
        current = self._store.get(url)
        if current is not None and not current.available:
            return _StagedKeyword(url=url, abstract=abstract)

        raw_content = hit.raw_content if hit.raw_content.strip() else ""
        content = hit.content if hit.content.strip() else ""
        candidate = content or raw_content
        if not candidate or not cheap_check(candidate):
            return _StagedKeyword(url=url, abstract=abstract)

        decision = await self._stages.judge(candidate)
        if not decision.ok:
            return _StagedKeyword(url=url, abstract=abstract)
        return _StagedKeyword(
            url=url,
            abstract=abstract,
            raw_content=raw_content,
            content=content,
        )

    def _record_from_store(self, url: NormalizedURL) -> SearchRecord:
        record = self._store.get(url)
        if record is None:
            raise RuntimeError("committed URL disappeared from store")
        return SearchRecord(record.url, record.abstract)
