import json
from pathlib import Path

import pytest

from agent_search_gateway.errors import ExecutionFailure
from agent_search_gateway.observability import SecretValue
from agent_search_gateway.providers.contracts import KeywordSearchHit, URLFetchCandidate
from agent_search_gateway.providers.web.linkup import LinkupAdapter
from agent_search_gateway.url_normalization import normalize_url
from tests.support.http import RecordingJsonExecutor

_FIXTURES = Path(__file__).parents[2] / "fixtures/providers/linkup"


def _fixture(name: str) -> object:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


async def test_linkup_adapter_maps_search_results_and_fetch_markdown() -> None:
    executor = RecordingJsonExecutor([_fixture("search.json"), _fixture("fetch.json")])
    adapter = LinkupAdapter(
        name="linkup",
        api_url="https://linkup.example.test/",
        secret=SecretValue("x"),
        http_executor=executor,
    )
    assert await adapter.search("query") == [
        KeywordSearchHit(
            "https://example.com/linkup",
            "Linkup result",
            "Linkup search snippet",
        )
    ]
    assert await adapter.fetch(normalize_url("https://example.com/linkup")) == URLFetchCandidate(
        "<article>Linkup raw</article>", "Linkup markdown"
    )

    search_request, fetch_request = executor.requests
    assert search_request.url == "https://linkup.example.test/v1/search"
    assert fetch_request.url == "https://linkup.example.test/v1/fetch"
    assert search_request.headers == {"Authorization": "Bearer x"}
    assert search_request.json_body == {
        "q": "query",
        "depth": "standard",
        "outputType": "searchResults",
    }
    assert fetch_request.json_body == {
        "url": "https://example.com/linkup",
        "includeRawHtml": True,
        "extractImages": False,
    }

    bad = LinkupAdapter(
        name="linkup",
        api_url="https://linkup.example.test",
        secret=SecretValue("x"),
        http_executor=RecordingJsonExecutor([{"markdown": "", "rawHtml": ""}]),
    )
    with pytest.raises(ExecutionFailure):
        await bad.fetch(normalize_url("https://example.com/linkup"))
