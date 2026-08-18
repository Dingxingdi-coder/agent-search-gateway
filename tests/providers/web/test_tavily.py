import json
from pathlib import Path

import pytest

from agent_search_gateway.errors import ExecutionFailure
from agent_search_gateway.observability import SecretValue
from agent_search_gateway.providers.contracts import KeywordSearchHit, URLFetchCandidate
from agent_search_gateway.providers.web.tavily import TavilyAdapter
from agent_search_gateway.url_normalization import normalize_url
from tests.support.http import RecordingJsonExecutor

_FIXTURES = Path(__file__).parents[2] / "fixtures/providers/tavily"


def _fixture(name: str) -> object:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


async def test_tavily_adapter_conforms_to_registered_search_and_fetch_contracts() -> None:
    executor = RecordingJsonExecutor([_fixture("search.json"), _fixture("extract.json")])
    adapter = TavilyAdapter(
        name="tavily",
        api_url="https://tavily.example.test/base/",
        secret=SecretValue("x"),
        http_executor=executor,
    )

    hits = await adapter.search("query")
    assert hits == [
        KeywordSearchHit(
            url="https://example.com/a",
            title="Example result",
            snippet="Search summary",
            raw_content="Full raw body",
            content="",
        )
    ]
    candidate = await adapter.fetch(normalize_url("https://example.com/a"))
    assert candidate == URLFetchCandidate("Extracted raw body", "")

    search_request, fetch_request = executor.requests
    assert search_request.url == "https://tavily.example.test/base/search"
    assert search_request.headers == {"Authorization": "Bearer x"}
    assert search_request.json_body == {"query": "query", "include_raw_content": "markdown"}
    assert fetch_request.url == "https://tavily.example.test/base/extract"
    assert fetch_request.json_body == {"urls": ["https://example.com/a"]}
    assert "URLStore" not in str(adapter.search.__annotations__)

    bad = TavilyAdapter(
        name="tavily",
        api_url="https://tavily.example.test",
        secret=SecretValue("x"),
        http_executor=RecordingJsonExecutor([{"results": []}]),
    )
    with pytest.raises(ExecutionFailure):
        await bad.fetch(normalize_url("https://example.com/a"))
