import json
from pathlib import Path

import pytest

from agent_search_gateway.errors import ExecutionFailure
from agent_search_gateway.observability import SecretValue
from agent_search_gateway.providers.contracts import KeywordSearchHit, URLFetchCandidate
from agent_search_gateway.providers.web.tinyfish import TinyFishAdapter
from agent_search_gateway.url_normalization import normalize_url
from tests.support.http import RecordingJsonExecutor

_FIXTURES = Path(__file__).parents[2] / "fixtures/providers/tinyfish"


def _fixture(name: str) -> object:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


async def test_tinyfish_adapter_uses_separate_search_and_fetch_base_urls() -> None:
    executor = RecordingJsonExecutor([_fixture("search.json"), _fixture("fetch.json")])
    adapter = TinyFishAdapter(
        name="tinyfish",
        search_api_url="https://search.tiny.example.test/",
        fetch_api_url="https://fetch.tiny.example.test/",
        secret=SecretValue("x"),
        http_executor=executor,
    )
    assert await adapter.search("hello world") == [
        KeywordSearchHit("https://example.com/tiny", "TinyFish result", "TinyFish snippet")
    ]
    assert await adapter.fetch(normalize_url("https://example.com/tiny")) == URLFetchCandidate(
        "TinyFish fetched markdown", "TinyFish fetched markdown"
    )

    search_request, fetch_request = executor.requests
    assert search_request.method == "GET"
    assert search_request.url == "https://search.tiny.example.test?query=hello+world"
    assert search_request.headers == {"X-API-Key": "x"}
    assert fetch_request.url == "https://fetch.tiny.example.test"
    assert fetch_request.headers == {"X-API-Key": "x"}
    assert fetch_request.json_body == {
        "urls": ["https://example.com/tiny"],
        "format": "markdown",
        "links": False,
        "image_links": False,
    }

    bad = TinyFishAdapter(
        name="tinyfish",
        search_api_url="https://search.tiny.example.test",
        fetch_api_url="https://fetch.tiny.example.test",
        secret=SecretValue("x"),
        http_executor=RecordingJsonExecutor(
            [
                {
                    "results": [],
                    "errors": [{"url": "https://example.com/tiny", "error": "timeout"}],
                }
            ]
        ),
    )
    with pytest.raises(ExecutionFailure):
        await bad.fetch(normalize_url("https://example.com/tiny"))
