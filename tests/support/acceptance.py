"""No-network runtime assembly used by acceptance tests."""

from collections.abc import Mapping, Sequence

from agent_search_gateway.concurrency import ProviderQuotaManager
from agent_search_gateway.llm.stages import LLMStages
from agent_search_gateway.models import LLMInvocation
from agent_search_gateway.orchestrators.fetch import FetchOrchestrator
from agent_search_gateway.orchestrators.search import SearchOrchestrator
from agent_search_gateway.paths import RuntimePaths
from agent_search_gateway.providers.contracts import (
    ChatMessage,
    KeywordSearchHit,
    URLFetchCandidate,
)
from agent_search_gateway.result_writer import ResultWriter
from agent_search_gateway.scheduler.fetch import FetchScheduler
from agent_search_gateway.url_store import URLStore
from tests.support.fakes import FakeKeywordSearchProvider, FakeURLFetchProvider


class _AcceptanceLLMClient:
    name = "llm"

    async def complete_json(
        self,
        invocation: LLMInvocation,
        messages: Sequence[ChatMessage],
    ) -> Mapping[str, object]:
        return {"ok": True}

    async def complete_text(
        self,
        invocation: LLMInvocation,
        messages: Sequence[ChatMessage],
    ) -> str:
        if invocation.model == "search-model":
            return "## Result\nURL: https://example.com/llm\nAbstract: LLM abstract\n"
        if invocation.model == "focus-model":
            prompt = messages[-1]["content"]
            focus = prompt.splitlines()[0].removeprefix("Focus: ")
            return f"Focused summary: {focus}"
        if invocation.model == "clean-model":
            return "Cleaned content"
        return "unused"

    async def aclose(self) -> None:
        return None


class AcceptanceRuntime:
    def __init__(self, paths: RuntimePaths) -> None:
        store = URLStore()
        keyword_provider = FakeKeywordSearchProvider(
            "keyword",
            [
                KeywordSearchHit(
                    url="https://example.com/article",
                    title="Article",
                    snippet="Keyword abstract",
                )
            ],
        )
        self.fetch_provider = FakeURLFetchProvider(
            "fetch",
            URLFetchCandidate(
                raw_content="Raw article body",
                content="Full article content",
            ),
        )
        quotas = ProviderQuotaManager(
            web_limits={"keyword": 2, "fetch": 2},
            llm_limits={},
        )
        client = _AcceptanceLLMClient()
        judge = LLMInvocation("llm", "judge-model", {})
        safety = LLMInvocation("llm", "safety-model", {})
        clean = LLMInvocation("llm", "clean-model", {})
        focus = LLMInvocation("llm", "focus-model", {})
        search = LLMInvocation("llm", "search-model", {})
        stages = LLMStages(
            {"llm": client},
            judge=judge,
            safety=safety,
            content_clean=clean,
            focus_summary=focus,
        )
        self.search_orchestrator = SearchOrchestrator(
            keyword_providers=[keyword_provider],
            llm_invocations=[search],
            quotas=quotas,
            stages=stages,
            store=store,
            result_writer=ResultWriter(paths.results_dir),
        )
        self.fetch_orchestrator = FetchOrchestrator(
            store=store,
            scheduler=FetchScheduler([self.fetch_provider], quotas, stages),
            stages=stages,
        )
        self.close_calls = 0

    async def aclose(self) -> None:
        self.close_calls += 1


def build_acceptance_runtime(paths: RuntimePaths) -> AcceptanceRuntime:
    return AcceptanceRuntime(paths)
