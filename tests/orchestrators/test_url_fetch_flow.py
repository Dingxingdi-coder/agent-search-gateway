from collections.abc import Mapping, Sequence

import pytest

from agent_search_gateway.concurrency import ProviderQuotaManager
from agent_search_gateway.errors import UNAVAILABLE_MESSAGE, ErrorCode, ExecutionFailure
from agent_search_gateway.llm.stages import LLMStages
from agent_search_gateway.models import LLMInvocation
from agent_search_gateway.orchestrators.fetch import FetchOrchestrator
from agent_search_gateway.providers.contracts import ChatMessage, URLFetchCandidate
from agent_search_gateway.scheduler.fetch import FetchScheduler
from agent_search_gateway.url_normalization import normalize_url
from agent_search_gateway.url_store import URLStore
from tests.support.fakes import FakeURLFetchProvider


class _FlowClient:
    name = "llm"

    def __init__(self) -> None:
        self.safety_ok = True
        self.fail_clean = False
        self.fail_safety = False
        self.fail_focus = False
        self.focus_calls = 0

    async def complete_json(
        self,
        invocation: LLMInvocation,
        messages: Sequence[ChatMessage],
    ) -> Mapping[str, object]:
        text = messages[-1]["content"]
        if invocation.model == "judge-model":
            if "semantic-reject" in text:
                return {"ok": False, "reason": "semantic"}
            return {"ok": True}
        if self.fail_safety:
            raise ExecutionFailure(ErrorCode.ALL_PROVIDERS_FAILED, "safety transport")
        return {"ok": self.safety_ok, "reason": "unsafe" if not self.safety_ok else ""}

    async def complete_text(
        self,
        invocation: LLMInvocation,
        messages: Sequence[ChatMessage],
    ) -> str:
        if invocation.model == "clean-model":
            if self.fail_clean:
                raise ExecutionFailure(ErrorCode.ALL_PROVIDERS_FAILED, "clean transport")
            return "cleaned body"
        if invocation.model == "focus-model":
            self.focus_calls += 1
            if self.fail_focus:
                raise ExecutionFailure(ErrorCode.ALL_PROVIDERS_FAILED, "focus transport")
            return f"summary-{self.focus_calls}"
        return "unused"

    async def aclose(self) -> None:
        return None


def _build(
    store: URLStore,
    client: _FlowClient,
    providers: list[FakeURLFetchProvider],
) -> FetchOrchestrator:
    judge = LLMInvocation("llm", "judge-model", {})
    safety = LLMInvocation("llm", "safety-model", {})
    clean = LLMInvocation("llm", "clean-model", {})
    focus = LLMInvocation("llm", "focus-model", {})
    stages = LLMStages(
        {"llm": client},
        judge=judge,
        safety=safety,
        content_clean=clean,
        focus_summary=focus,
    )
    scheduler = FetchScheduler(
        providers,
        ProviderQuotaManager(
            web_limits={provider.name: 1 for provider in providers},
            llm_limits={},
        ),
        stages,
    )
    return FetchOrchestrator(store=store, scheduler=scheduler, stages=stages)


def _admit(store: URLStore, suffix: str) -> str:
    url = normalize_url(f"https://example.com/{suffix}")
    store.admit(url, "known")
    return str(url)


async def test_url_fetch_mutates_state_only_for_accepted_or_semantically_rejected_results() -> None:
    store = URLStore()
    client = _FlowClient()
    accepted_url = _admit(store, "accepted")
    accepted_provider = FakeURLFetchProvider(
        "accepted",
        URLFetchCandidate(raw_content="raw accepted", content="provider clean"),
    )
    accepted_orchestrator = _build(store, client, [accepted_provider])
    assert await accepted_orchestrator.url_fetch(accepted_url) == "provider clean"
    accepted = store.get(normalize_url(accepted_url))
    assert accepted is not None
    assert accepted.raw_content == "raw accepted"
    assert accepted.content == "provider clean"
    assert accepted.available is True

    raw_store = URLStore()
    raw_client = _FlowClient()
    raw_url = _admit(raw_store, "raw-only")
    raw_orchestrator = _build(
        raw_store,
        raw_client,
        [FakeURLFetchProvider("raw", URLFetchCandidate(raw_content="raw only"))],
    )
    assert await raw_orchestrator.url_fetch(raw_url) == "cleaned body"
    raw_snapshot = raw_store.get(normalize_url(raw_url))
    assert raw_snapshot is not None
    assert raw_snapshot.raw_content == "raw only"
    assert raw_snapshot.content == "cleaned body"

    execution_store = URLStore()
    execution_url = _admit(execution_store, "execution")
    execution_orchestrator = _build(
        execution_store,
        _FlowClient(),
        [
            FakeURLFetchProvider(
                "failure",
                failure=ExecutionFailure(ErrorCode.ALL_PROVIDERS_FAILED, "provider execution"),
            )
        ],
    )
    with pytest.raises(ExecutionFailure) as execution:
        await execution_orchestrator.url_fetch(execution_url)
    assert execution.value.code is ErrorCode.ALL_PROVIDERS_FAILED
    execution_snapshot = execution_store.get(normalize_url(execution_url))
    assert execution_snapshot is not None and execution_snapshot.available is True

    semantic_store = URLStore()
    semantic_url = _admit(semantic_store, "semantic")
    semantic_orchestrator = _build(
        semantic_store,
        _FlowClient(),
        [FakeURLFetchProvider("semantic", URLFetchCandidate(raw_content="semantic-reject"))],
    )
    assert await semantic_orchestrator.url_fetch(semantic_url) == UNAVAILABLE_MESSAGE
    semantic_snapshot = semantic_store.get(normalize_url(semantic_url))
    assert semantic_snapshot is not None and semantic_snapshot.available is False

    safety_store = URLStore()
    safety_url = _admit(safety_store, "safety")
    safety_client = _FlowClient()
    safety_client.safety_ok = False
    safety_orchestrator = _build(
        safety_store,
        safety_client,
        [FakeURLFetchProvider("safety", URLFetchCandidate(raw_content="raw", content="content"))],
    )
    assert await safety_orchestrator.url_fetch(safety_url) == UNAVAILABLE_MESSAGE
    safety_snapshot = safety_store.get(normalize_url(safety_url))
    assert safety_snapshot is not None and safety_snapshot.available is False

    safety_execution_store = URLStore()
    safety_execution_url = _admit(safety_execution_store, "safety-execution")
    safety_execution_client = _FlowClient()
    safety_execution_client.fail_safety = True
    safety_execution_orchestrator = _build(
        safety_execution_store,
        safety_execution_client,
        [FakeURLFetchProvider("ok", URLFetchCandidate(raw_content="raw", content="content"))],
    )
    with pytest.raises(ExecutionFailure) as safety_execution:
        await safety_execution_orchestrator.url_fetch(safety_execution_url)
    assert safety_execution.value.code is ErrorCode.LLM_STAGE_FAILED
    safety_execution_snapshot = safety_execution_store.get(normalize_url(safety_execution_url))
    assert safety_execution_snapshot is not None and safety_execution_snapshot.available is True

    clean_store = URLStore()
    clean_url = _admit(clean_store, "clean-execution")
    clean_client = _FlowClient()
    clean_client.fail_clean = True
    clean_orchestrator = _build(
        clean_store,
        clean_client,
        [FakeURLFetchProvider("raw", URLFetchCandidate(raw_content="raw only"))],
    )
    with pytest.raises(ExecutionFailure) as clean_execution:
        await clean_orchestrator.url_fetch(clean_url)
    assert clean_execution.value.code is ErrorCode.LLM_STAGE_FAILED
    clean_snapshot = clean_store.get(normalize_url(clean_url))
    assert clean_snapshot is not None and clean_snapshot.available is True
    assert clean_snapshot.raw_content == "raw only" and clean_snapshot.content == ""

    focus_store = URLStore()
    focus_url = _admit(focus_store, "focus")
    focus_client = _FlowClient()
    focus_orchestrator = _build(
        focus_store,
        focus_client,
        [FakeURLFetchProvider("focus", URLFetchCandidate(raw_content="raw", content="content"))],
    )
    assert await focus_orchestrator.url_fetch(focus_url, "pricing") == "summary-1"
    assert await focus_orchestrator.url_fetch(focus_url, "pricing") == "summary-2"
    focus_snapshot = focus_store.get(normalize_url(focus_url))
    assert focus_snapshot is not None and focus_snapshot.content == "content"
    focus_client.fail_focus = True
    with pytest.raises(ExecutionFailure) as focus_failure:
        await focus_orchestrator.url_fetch(focus_url, "pricing")
    assert focus_failure.value.code is ErrorCode.LLM_STAGE_FAILED
