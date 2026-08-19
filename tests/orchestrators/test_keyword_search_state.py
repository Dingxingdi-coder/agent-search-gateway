import asyncio
import json
from collections.abc import Mapping, Sequence
from pathlib import Path

from agent_search_gateway.concurrency import ProviderQuotaManager
from agent_search_gateway.errors import ErrorCode, ExecutionFailure
from agent_search_gateway.llm.stages import LLMStages
from agent_search_gateway.models import LLMInvocation
from agent_search_gateway.orchestrators.search import SearchOrchestrator
from agent_search_gateway.providers.contracts import ChatMessage, KeywordSearchHit
from agent_search_gateway.result_writer import ResultWriter
from agent_search_gateway.url_normalization import normalize_url
from agent_search_gateway.url_store import URLStore


class _JudgeClient:
    name = "judge"

    def __init__(self) -> None:
        self.candidates: list[str] = []

    async def complete_json(
        self,
        invocation: LLMInvocation,
        messages: Sequence[ChatMessage],
    ) -> Mapping[str, object]:
        candidate = messages[-1]["content"]
        self.candidates.append(candidate)
        if "explode-body" in candidate:
            raise ExecutionFailure(ErrorCode.ALL_PROVIDERS_FAILED, "judge exploded")
        if "reject-body" in candidate:
            return {"ok": False, "reason": "not usable"}
        return {"ok": True}

    async def complete_text(
        self,
        invocation: LLMInvocation,
        messages: Sequence[ChatMessage],
    ) -> str:
        return "unused"

    async def aclose(self) -> None:
        return None


class _OrderedProvider:
    def __init__(
        self,
        name: str,
        hits: list[KeywordSearchHit],
        *,
        wait_for: asyncio.Event | None = None,
        signal: asyncio.Event | None = None,
    ) -> None:
        self.name = name
        self.hits = hits
        self.wait_for = wait_for
        self.signal = signal
        self.calls = 0

    async def search(self, query: str) -> list[KeywordSearchHit]:
        self.calls += 1
        if self.signal is not None:
            self.signal.set()
        if self.wait_for is not None:
            await self.wait_for.wait()
        return list(self.hits)


async def test_keyword_search_validates_body_then_commits_deterministic_first_write_state(
    tmp_path: Path,
) -> None:
    store = URLStore()
    unavailable_url = normalize_url("https://example.com/unavailable")
    store.admit(unavailable_url, "Stored unavailable", raw_content="old raw")
    store.mark_unavailable(unavailable_url)

    judge_client = _JudgeClient()
    invocation = LLMInvocation("judge", "model", {})
    stages = LLMStages(
        {"judge": judge_client},
        judge=invocation,
        safety=invocation,
        content_clean=invocation,
        focus_summary=invocation,
    )

    release_first = asyncio.Event()
    first_provider = _OrderedProvider(
        "first",
        [
            KeywordSearchHit(
                "https://EXAMPLE.com/a",
                title="Title A",
                snippet="First abstract",
                raw_content="raw-a",
                content="content-a",
            ),
            KeywordSearchHit("https://example.com/skip", title=" ", snippet=""),
            KeywordSearchHit(
                "https://example.com/raw",
                title="Raw title",
                raw_content="raw-only",
            ),
            KeywordSearchHit(
                "https://example.com/reject",
                snippet="Rejected body still admitted",
                raw_content="reject-body",
            ),
            KeywordSearchHit(
                "https://example.com/unavailable",
                snippet="New unavailable abstract",
                content="explode-body-unavailable-must-be-skipped",
            ),
            KeywordSearchHit(
                "https://example.com/a",
                snippet="Duplicate later abstract",
                raw_content="raw-later",
                content="content-later",
            ),
        ],
        wait_for=release_first,
    )
    second_provider = _OrderedProvider(
        "second",
        [
            KeywordSearchHit(
                "https://example.com/a",
                snippet="Second provider abstract",
                raw_content="raw-b",
                content="content-b",
            ),
            KeywordSearchHit("https://example.com/b", snippet="B abstract"),
        ],
        signal=release_first,
    )
    failed_provider = _OrderedProvider(
        "failed",
        [
            KeywordSearchHit("https://example.com/c", snippet="C would be staged"),
            KeywordSearchHit(
                "https://example.com/explode",
                snippet="Explodes judge",
                raw_content="explode-body",
            ),
        ],
    )
    providers = [first_provider, second_provider, failed_provider]
    orchestrator = SearchOrchestrator(
        keyword_providers=providers,
        llm_invocations=(),
        quotas=ProviderQuotaManager(
            web_limits={provider.name: 1 for provider in providers},
            llm_limits={},
        ),
        stages=stages,
        store=store,
        result_writer=ResultWriter(tmp_path / "results"),
    )

    first_path = Path(await orchestrator.keyword_search("query"))
    output = [json.loads(line) for line in first_path.read_text(encoding="utf-8").splitlines()]
    assert output == [
        {"url": "https://example.com/a", "abstract": "First abstract"},
        {"url": "https://example.com/raw", "abstract": "Raw title"},
        {"url": "https://example.com/reject", "abstract": "Rejected body still admitted"},
        {"url": "https://example.com/unavailable", "abstract": "Stored unavailable"},
        {"url": "https://example.com/b", "abstract": "B abstract"},
    ]

    a = store.get(normalize_url("https://example.com/a"))
    assert a is not None
    assert (a.raw_content, a.content, a.abstract) == ("raw-a", "content-a", "First abstract")
    raw = store.get(normalize_url("https://example.com/raw"))
    assert raw is not None and raw.raw_content == "raw-only" and raw.content == ""
    rejected = store.get(normalize_url("https://example.com/reject"))
    assert rejected is not None and rejected.raw_content == "" and rejected.available is True
    assert store.get(normalize_url("https://example.com/skip")) is None
    assert store.get(normalize_url("https://example.com/c")) is None
    assert "explode-body-unavailable-must-be-skipped" not in "\n".join(judge_client.candidates)

    second_path = Path(await orchestrator.keyword_search("query"))
    assert second_path != first_path
    assert [provider.calls for provider in providers] == [2, 2, 2]
