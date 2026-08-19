from pathlib import Path

import pytest

from agent_search_gateway.concurrency import ProviderQuotaManager
from agent_search_gateway.errors import ErrorCode, ExecutionFailure, InputFailure
from agent_search_gateway.llm.stages import LLMStages
from agent_search_gateway.models import LLMInvocation
from agent_search_gateway.orchestrators.search import SearchOrchestrator
from agent_search_gateway.providers.contracts import KeywordSearchHit
from agent_search_gateway.result_writer import ResultWriter
from agent_search_gateway.url_store import URLStore
from tests.support.fakes import FakeKeywordSearchProvider, FakeLLMClient


def _stages() -> LLMStages:
    invocation = LLMInvocation("llm", "model", {})
    client = FakeLLMClient("llm", json_result={"ok": True})
    return LLMStages(
        {"llm": client},
        judge=invocation,
        safety=invocation,
        content_clean=invocation,
        focus_summary=invocation,
    )


def _orchestrator(
    tmp_path: Path,
    providers: list[FakeKeywordSearchProvider],
    store: URLStore | None = None,
) -> SearchOrchestrator:
    return SearchOrchestrator(
        keyword_providers=providers,
        llm_invocations=(),
        quotas=ProviderQuotaManager(
            web_limits={provider.name: 1 for provider in providers},
            llm_limits={},
        ),
        stages=_stages(),
        store=store or URLStore(),
        result_writer=ResultWriter(tmp_path / "results"),
    )


async def test_keyword_search_uses_pipeline_completion_rules(tmp_path: Path) -> None:
    with pytest.raises(InputFailure) as empty:
        await _orchestrator(tmp_path, []).keyword_search("   ")
    assert empty.value.code is ErrorCode.EMPTY_QUERY

    with pytest.raises(ExecutionFailure) as absent:
        await _orchestrator(tmp_path, []).keyword_search("query")
    assert absent.value.code is ErrorCode.NO_KEYWORD_SEARCH_PROVIDERS

    failed = FakeKeywordSearchProvider(
        "failed",
        failure=ExecutionFailure(ErrorCode.ALL_PROVIDERS_FAILED, "provider failed"),
    )
    success = FakeKeywordSearchProvider(
        "success",
        [KeywordSearchHit("https://example.com/a", snippet="A")],
    )
    orchestrator = _orchestrator(tmp_path, [failed, success])
    path = Path(await orchestrator.keyword_search(" query "))
    assert path.exists()
    assert path.read_text(encoding="utf-8") == '{"url":"https://example.com/a","abstract":"A"}\n'
    assert failed.calls == ["query"]
    assert success.calls == ["query"]
    assert orchestrator.quotas.get_web("failed").max_observed_in_use == 1
    assert orchestrator.quotas.get_web("success").max_observed_in_use == 1

    empty_provider = FakeKeywordSearchProvider("empty", [])
    empty_path = Path(await _orchestrator(tmp_path, [empty_provider]).keyword_search("query"))
    assert empty_path.exists()
    assert empty_path.read_text(encoding="utf-8") == ""

    store = URLStore()
    all_failed = _orchestrator(
        tmp_path,
        [
            FakeKeywordSearchProvider(
                "one",
                failure=ExecutionFailure(ErrorCode.ALL_PROVIDERS_FAILED, "one"),
            ),
            FakeKeywordSearchProvider(
                "two",
                failure=ExecutionFailure(ErrorCode.ALL_PROVIDERS_FAILED, "two"),
            ),
        ],
        store,
    )
    before = set((tmp_path / "results").glob("keyword-*.jsonl"))
    with pytest.raises(ExecutionFailure) as all_failed_error:
        await all_failed.keyword_search("query")
    after = set((tmp_path / "results").glob("keyword-*.jsonl"))
    assert all_failed_error.value.code is ErrorCode.ALL_PROVIDERS_FAILED
    assert after == before
