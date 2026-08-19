"""Sequential URL-fetch provider scheduler with typed outcomes."""

import asyncio
from collections.abc import Sequence

from ..concurrency import CapacityLease, ProviderQuotaManager
from ..errors import ErrorCode, ExecutionFailure
from ..llm.stages import LLMStages, cheap_check
from ..models import FetchOutcome
from ..providers.contracts import URLFetchCandidate, URLFetchProvider
from ..url_normalization import NormalizedURL


class FetchScheduler:
    def __init__(
        self,
        providers: Sequence[URLFetchProvider],
        quotas: ProviderQuotaManager,
        stages: LLMStages,
    ) -> None:
        self._providers = tuple(providers)
        self._quotas = quotas
        self._stages = stages

    @property
    def provider_names(self) -> tuple[str, ...]:
        return tuple(provider.name for provider in self._providers)

    async def fetch_until_accepted(self, url: NormalizedURL) -> FetchOutcome:
        semantic_failure_seen = False
        failures: list[ExecutionFailure] = []
        remaining = list(self._providers)
        while remaining:
            provider, lease = await self._select_available_provider(remaining)
            remaining.remove(provider)
            try:
                async with lease:
                    outcome = await self._attempt(provider, url)
            except asyncio.CancelledError:
                raise
            if outcome.kind == "accepted":
                return FetchOutcome("accepted", outcome.candidate, tuple(failures))
            if outcome.kind == "semantic_failure":
                semantic_failure_seen = True
            else:
                failures.extend(outcome.failures)

        if semantic_failure_seen:
            return FetchOutcome("semantic_failure", failures=tuple(failures))
        return FetchOutcome("execution_failure", failures=tuple(failures))

    async def _select_available_provider(
        self,
        remaining: list[URLFetchProvider],
    ) -> tuple[URLFetchProvider, CapacityLease]:
        while True:
            for provider in remaining:
                lease = await self._quotas.get_web(provider.name).try_lease()
                if lease is not None:
                    return provider, lease
            await self._quotas.wait_until_any_web_available(
                tuple(provider.name for provider in remaining)
            )

    async def _attempt(
        self,
        provider: URLFetchProvider,
        url: NormalizedURL,
    ) -> FetchOutcome:
        try:
            candidate = await provider.fetch(url)
            self._validate_candidate(candidate)
            validation_candidate = (
                candidate.content if candidate.content != "" else candidate.raw_content
            )
            if not cheap_check(validation_candidate):
                return FetchOutcome("semantic_failure")
            decision = await self._stages.judge(validation_candidate)
            if not decision.ok:
                return FetchOutcome("semantic_failure")
            return FetchOutcome("accepted", candidate)
        except asyncio.CancelledError:
            raise
        except ExecutionFailure as exc:
            return FetchOutcome("execution_failure", failures=(exc,))
        except Exception:
            failure = ExecutionFailure(
                ErrorCode.ALL_PROVIDERS_FAILED,
                f"Fetch provider {provider.name} returned invalid data",
            )
            return FetchOutcome("execution_failure", failures=(failure,))

    @staticmethod
    def _validate_candidate(candidate: URLFetchCandidate) -> None:
        if not isinstance(candidate, URLFetchCandidate):
            raise TypeError("fetch candidate has invalid type")
        if not isinstance(candidate.raw_content, str) or not isinstance(candidate.content, str):
            raise TypeError("fetch candidate fields must be strings")
        if candidate.raw_content == "":
            raise ValueError("fetch candidate raw_content must be non-empty")
