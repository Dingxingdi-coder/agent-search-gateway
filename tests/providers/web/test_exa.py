import json
from pathlib import Path

import pytest

from agent_search_gateway.errors import ExecutionFailure
from agent_search_gateway.observability import SecretValue
from agent_search_gateway.providers.contracts import KeywordSearchHit, URLFetchCandidate
from agent_search_gateway.providers.web.exa import ExaAdapter
from agent_search_gateway.url_normalization import normalize_url
from tests.support.http import RecordingJsonExecutor

_FIXTURES = Path(__file__).parents[2] / "fixtures/providers/exa"


def _fixture(name: str) -> object:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


async def test_exa_adapter_maps_search_and_contents_without_deprecated_fields() -> None:
    executor = RecordingJsonExecutor([_fixture("search.json"), _fixture("contents.json")])
    adapter = ExaAdapter(
        name="exa",
        api_url="https://exa.example.test/",
        secret=SecretValue("x"),
        http_executor=executor,
    )
    hits = await adapter.search("query")
    assert hits == [
        KeywordSearchHit(
            "https://example.com/exa",
            "Exa result",
            "First highlight",
            "Full Exa text",
            "Full Exa text",
        )
    ]
    candidate = await adapter.fetch(normalize_url("https://example.com/exa"))
    assert candidate == URLFetchCandidate("Fetched Exa text", "Fetched Exa text")

    search_request, fetch_request = executor.requests
    assert search_request.url == "https://exa.example.test/search"
    assert search_request.headers == {"x-api-key": "x"}
    assert search_request.json_body == {
        "query": "query",
        "contents": {"text": True, "highlights": True},
    }
    assert fetch_request.url == "https://exa.example.test/contents"
    assert fetch_request.json_body == {"urls": ["https://example.com/exa"], "text": True}
    for deprecated in ("context", "livecrawl", "tokensNum"):
        assert deprecated not in str(search_request.json_body)
        assert deprecated not in str(fetch_request.json_body)

    bad = ExaAdapter(
        name="exa",
        api_url="https://exa.example.test",
        secret=SecretValue("x"),
        http_executor=RecordingJsonExecutor(
            [
                {
                    "results": [{"url": "https://example.com/exa", "text": "body"}],
                    "statuses": [{"id": "https://example.com/exa", "status": "error"}],
                }
            ]
        ),
    )
    with pytest.raises(ExecutionFailure):
        await bad.fetch(normalize_url("https://example.com/exa"))
