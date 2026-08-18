from collections.abc import Mapping, Sequence

from agent_search_gateway.concurrency import ProviderQuotaManager
from agent_search_gateway.errors import ErrorCode, ExecutionFailure
from agent_search_gateway.llm.stages import LLMStages
from agent_search_gateway.models import LLMInvocation
from agent_search_gateway.providers.contracts import ChatMessage, URLFetchCandidate
from agent_search_gateway.scheduler.fetch import FetchScheduler
from agent_search_gateway.url_normalization import normalize_url
from tests.support.fakes import FakeURLFetchProvider


class _JudgeClient:
    name = "judge"

    async def complete_json(
        self,
        invocation: LLMInvocation,
        messages: Sequence[ChatMessage],
    ) -> Mapping[str, object]:
        content = messages[-1]["content"]
        if "judge-execution" in content:
            raise ExecutionFailure(ErrorCode.ALL_PROVIDERS_FAILED, "judge failed")
        if "judge-reject" in content:
            return {"ok": False, "reason": "rejected"}
        return {"ok": True}

    async def complete_text(
        self,
        invocation: LLMInvocation,
        messages: Sequence[ChatMessage],
    ) -> str:
        return "unused"

    async def aclose(self) -> None:
        return None


def _scheduler(providers: list[FakeURLFetchProvider]) -> FetchScheduler:
    invocation = LLMInvocation("judge", "model", {})
    stages = LLMStages(
        {"judge": _JudgeClient()},
        judge=invocation,
        safety=invocation,
        content_clean=invocation,
        focus_summary=invocation,
    )
    return FetchScheduler(
        providers,
        ProviderQuotaManager(
            web_limits={provider.name: 1 for provider in providers},
            llm_limits={},
        ),
        stages,
    )


async def test_fetch_scheduler_classifies_execution_semantic_and_accepted_outcomes() -> None:
    url = normalize_url("https://example.com")

    failed = FakeURLFetchProvider(
        "failed",
        failure=ExecutionFailure(ErrorCode.ALL_PROVIDERS_FAILED, "provider failed"),
    )
    accepted = FakeURLFetchProvider(
        "accepted",
        URLFetchCandidate(raw_content="raw", content="clean"),
    )
    result = await _scheduler([failed, accepted]).fetch_until_accepted(url)
    assert result.kind == "accepted"
    assert result.candidate == URLFetchCandidate("raw", "clean")
    assert len(result.failures) == 1
    assert failed.calls == [url]
    assert accepted.calls == [url]

    malformed = FakeURLFetchProvider("malformed", URLFetchCandidate(raw_content=""))
    malformed_result = await _scheduler([malformed]).fetch_until_accepted(url)
    assert malformed_result.kind == "execution_failure"
    assert len(malformed_result.failures) == 1

    cheap_semantic = FakeURLFetchProvider("cheap", URLFetchCandidate(raw_content="   "))
    semantic_result = await _scheduler([cheap_semantic]).fetch_until_accepted(url)
    assert semantic_result.kind == "semantic_failure"

    judge_semantic = FakeURLFetchProvider(
        "judge-semantic",
        URLFetchCandidate(raw_content="judge-reject"),
    )
    semantic_result = await _scheduler([judge_semantic]).fetch_until_accepted(url)
    assert semantic_result.kind == "semantic_failure"

    judge_execution = FakeURLFetchProvider(
        "judge-execution",
        URLFetchCandidate(raw_content="judge-execution"),
    )
    later_success = FakeURLFetchProvider("later", URLFetchCandidate(raw_content="later-success"))
    recovered = await _scheduler([judge_execution, later_success]).fetch_until_accepted(url)
    assert recovered.kind == "accepted"
    assert recovered.candidate == URLFetchCandidate("later-success")
    assert len(recovered.failures) == 1

    mixed = await _scheduler(
        [
            FakeURLFetchProvider(
                "bad", failure=ExecutionFailure(ErrorCode.ALL_PROVIDERS_FAILED, "bad")
            ),
            FakeURLFetchProvider("semantic", URLFetchCandidate(raw_content="judge-reject")),
        ]
    ).fetch_until_accepted(url)
    assert mixed.kind == "semantic_failure"

    first_success = FakeURLFetchProvider("first", URLFetchCandidate(raw_content="first"))
    never_called = FakeURLFetchProvider("never", URLFetchCandidate(raw_content="second"))
    stopped = await _scheduler([first_success, never_called]).fetch_until_accepted(url)
    assert stopped.kind == "accepted"
    assert never_called.calls == []
