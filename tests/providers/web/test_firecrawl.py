import json
from pathlib import Path

import pytest

from agent_search_gateway.errors import ExecutionFailure
from agent_search_gateway.observability import SecretValue
from agent_search_gateway.providers.contracts import KeywordSearchHit, URLFetchCandidate
from agent_search_gateway.providers.web.firecrawl import FirecrawlAdapter
from agent_search_gateway.url_normalization import normalize_url
from tests.support.http import RecordingJsonExecutor

_FIXTURES = Path(__file__).parents[2] / "fixtures/providers/firecrawl"


def _fixture(name: str) -> object:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


async def test_firecrawl_adapter_maps_v2_search_and_scrape_payloads() -> None:
    executor = RecordingJsonExecutor([_fixture("search.json"), _fixture("scrape.json")])
    adapter = FirecrawlAdapter(
        name="firecrawl",
        api_url="https://fire.example.test/v2",
        secret=SecretValue("x"),
        http_executor=executor,
    )
    hits = await adapter.search("query")
    assert hits == [
        KeywordSearchHit(
            "https://example.com/fire",
            "Firecrawl result",
            "Firecrawl snippet",
            "<main>Raw HTML</main>",
            "Clean markdown",
        )
    ]
    candidate = await adapter.fetch(normalize_url("https://example.com/fire"))
    assert candidate == URLFetchCandidate("<article>Fetched raw</article>", "Fetched markdown")

    search_request, fetch_request = executor.requests
    assert search_request.url == "https://fire.example.test/v2/search"
    assert fetch_request.url == "https://fire.example.test/v2/scrape"
    assert search_request.headers == {"Authorization": "Bearer x"}
    assert search_request.json_body == {
        "query": "query",
        "sources": ["web"],
        "scrapeOptions": {"formats": ["markdown", "rawHtml"]},
    }
    assert fetch_request.json_body == {
        "url": "https://example.com/fire",
        "formats": ["markdown", "rawHtml"],
    }

    markdown_only = FirecrawlAdapter(
        name="firecrawl",
        api_url="https://fire.example.test",
        secret=SecretValue("x"),
        http_executor=RecordingJsonExecutor(
            [{"success": True, "data": {"markdown": "only markdown"}}]
        ),
    )
    assert await markdown_only.fetch(
        normalize_url("https://example.com/fire")
    ) == URLFetchCandidate("only markdown", "only markdown")

    bad = FirecrawlAdapter(
        name="firecrawl",
        api_url="https://fire.example.test",
        secret=SecretValue("x"),
        http_executor=RecordingJsonExecutor([{"success": False}]),
    )
    with pytest.raises(ExecutionFailure):
        await bad.search("query")
