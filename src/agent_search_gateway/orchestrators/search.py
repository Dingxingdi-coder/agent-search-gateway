"""Keyword and LLM search workflow orchestration."""

import asyncio
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from ..concurrency import ProviderQuotaManager
from ..errors import ErrorCode, ExecutionFailure, InputFailure
from ..llm.stages import LLMStages, cheap_check
from ..llm_search_parser import parse_search_markdown
from ..models import LLMInvocation, SearchRecord
from ..observability import elapsed_ms, log_event, target_url_for_log
from ..providers.contracts import KeywordSearchHit, KeywordSearchProvider
from ..request_ids import validate_request_id
from ..result_writer import ResultWriter
from ..url_normalization import NormalizedURL, normalize_url
from ..url_store import URLStore


@dataclass(frozen=True, slots=True)
class _StagedKeyword:
    url: NormalizedURL
    abstract: str
    provider: str
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
        logger: logging.Logger | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._keyword_providers = tuple(keyword_providers)
        self._llm_invocations = tuple(llm_invocations)
        self.quotas = quotas
        self._stages = stages
        self._store = store
        self._result_writer = result_writer
        self._logger = logger or logging.getLogger(__name__)
        self._monotonic = monotonic

    async def keyword_search(self, query: str, *, request_id: str) -> str:
        validate_request_id(request_id)
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
                else:
                    log_event(
                        self._logger,
                        logging.DEBUG,
                        "candidate_rejected",
                        provider=staged.provider,
                        url=target_url_for_log(str(staged.url)),
                        reason="duplicate",
                    )

        records = [self._record_from_store(url) for url in ordered_urls]
        path = self._result_writer.write_results("keyword", records, request_id=request_id)
        log_event(
            self._logger,
            logging.DEBUG,
            "results_written",
            kind="keyword",
            path=str(path),
            results=len(records),
        )
        return str(path)

    async def llm_search(self, prompt: str, *, request_id: str) -> str:
        validate_request_id(request_id)
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
        path = self._result_writer.write_results("llm", records, request_id=request_id)
        log_event(
            self._logger,
            logging.DEBUG,
            "results_written",
            kind="llm",
            path=str(path),
            results=len(records),
        )
        return str(path)

    async def _run_llm_pipeline(
        self,
        invocation: LLMInvocation,
        prompt: str,
    ) -> list[SearchRecord]:
        started = self._monotonic()
        log_event(
            self._logger,
            logging.DEBUG,
            "provider_started",
            provider=invocation.provider,
            stage="llm_search",
            model=invocation.model,
        )
        try:
            markdown = await self._stages.llm_search_markdown(invocation, prompt)
            records = parse_search_markdown(markdown)
        except asyncio.CancelledError:
            raise
        except ExecutionFailure as exc:
            self._log_provider_failure(invocation.provider, "llm_search", started, exc)
            raise
        except Exception as exc:
            self._log_provider_failure(invocation.provider, "llm_search", started, exc)
            raise ExecutionFailure(
                ErrorCode.ALL_PROVIDERS_FAILED,
                f"LLM search provider {invocation.provider} returned invalid data",
            ) from exc
        log_event(
            self._logger,
            logging.DEBUG,
            "provider_completed",
            provider=invocation.provider,
            stage="llm_search",
            model=invocation.model,
            output_chars=len(markdown),
            results=len(records),
            elapsed_ms=elapsed_ms(self._monotonic, started),
        )
        return records

    async def _run_keyword_pipeline(
        self,
        provider: KeywordSearchProvider,
        query: str,
    ) -> list[_StagedKeyword]:
        started = self._monotonic()
        log_event(
            self._logger,
            logging.DEBUG,
            "provider_started",
            provider=provider.name,
            stage="search",
        )
        try:
            async with self.quotas.get_web(provider.name).lease():
                hits = await provider.search(query)
            if not isinstance(hits, list):
                raise TypeError("provider search result must be a list")
            staged: list[_StagedKeyword] = []
            for hit in hits:
                staged_hit = await self._stage_keyword_hit(hit, provider=provider.name)
                if staged_hit is not None:
                    staged.append(staged_hit)
        except asyncio.CancelledError:
            raise
        except ExecutionFailure as exc:
            self._log_provider_failure(provider.name, "search", started, exc)
            raise
        except Exception as exc:
            self._log_provider_failure(provider.name, "search", started, exc)
            raise ExecutionFailure(
                ErrorCode.ALL_PROVIDERS_FAILED,
                f"Keyword provider {provider.name} returned invalid data",
            ) from exc
        log_event(
            self._logger,
            logging.DEBUG,
            "provider_completed",
            provider=provider.name,
            stage="search",
            hits=len(hits),
            results=len(staged),
            elapsed_ms=elapsed_ms(self._monotonic, started),
        )
        return staged

    async def _stage_keyword_hit(
        self,
        hit: KeywordSearchHit,
        *,
        provider: str,
    ) -> _StagedKeyword | None:
        if not isinstance(hit, KeywordSearchHit):
            raise TypeError("keyword hit has invalid type")
        for value in (hit.url, hit.title, hit.snippet, hit.raw_content, hit.content):
            if not isinstance(value, str):
                raise TypeError("keyword hit fields must be strings")

        abstract = hit.snippet.strip() or hit.title.strip()
        if not abstract:
            try:
                logged_url = target_url_for_log(str(normalize_url(hit.url)))
            except InputFailure:
                logged_url = target_url_for_log(hit.url)
            log_event(
                self._logger,
                logging.DEBUG,
                "candidate_rejected",
                provider=provider,
                url=logged_url,
                reason="empty_abstract",
            )
            return None
        url = normalize_url(hit.url)
        log_event(
            self._logger,
            logging.DEBUG,
            "candidate_accepted",
            provider=provider,
            url=target_url_for_log(str(url)),
            abstract_chars=len(abstract),
        )
        current = self._store.get(url)
        if current is not None and not current.available:
            self._log_body_decision(provider, url, "body_skipped", "stored_unavailable")
            return _StagedKeyword(url=url, abstract=abstract, provider=provider)

        had_body = bool(hit.raw_content or hit.content)
        raw_content = hit.raw_content if hit.raw_content.strip() else ""
        content = hit.content if hit.content.strip() else ""
        candidate = content or raw_content
        if not candidate:
            reason = "cheap_check" if had_body else "no_body"
            event = "body_rejected" if had_body else "body_skipped"
            self._log_body_decision(provider, url, event, reason)
            return _StagedKeyword(url=url, abstract=abstract, provider=provider)
        if not cheap_check(candidate):
            self._log_body_decision(provider, url, "body_rejected", "cheap_check")
            return _StagedKeyword(url=url, abstract=abstract, provider=provider)

        decision = await self._stages.judge(candidate)
        if not decision.ok:
            self._log_body_decision(provider, url, "body_rejected", "judge_rejected")
            return _StagedKeyword(url=url, abstract=abstract, provider=provider)
        log_event(
            self._logger,
            logging.DEBUG,
            "body_accepted",
            provider=provider,
            url=target_url_for_log(str(url)),
            raw_chars=len(raw_content),
            content_chars=len(content),
        )
        return _StagedKeyword(
            url=url,
            abstract=abstract,
            provider=provider,
            raw_content=raw_content,
            content=content,
        )

    def _log_body_decision(
        self,
        provider: str,
        url: NormalizedURL,
        event: str,
        reason: str,
    ) -> None:
        log_event(
            self._logger,
            logging.DEBUG,
            event,
            provider=provider,
            url=target_url_for_log(str(url)),
            reason=reason,
        )

    def _log_provider_failure(
        self,
        provider: str,
        stage: str,
        started: float,
        exc: Exception,
    ) -> None:
        log_event(
            self._logger,
            logging.DEBUG,
            "provider_failed",
            provider=provider,
            stage=stage,
            error_type=type(exc).__name__,
            elapsed_ms=elapsed_ms(self._monotonic, started),
        )

    def _record_from_store(self, url: NormalizedURL) -> SearchRecord:
        record = self._store.get(url)
        if record is None:
            raise RuntimeError("committed URL disappeared from store")
        return SearchRecord(record.url, record.abstract)
