import json
from pathlib import Path

import pytest

from agent_search_gateway.errors import ExecutionFailure
from agent_search_gateway.observability import SecretValue
from agent_search_gateway.providers.contracts import KeywordSearchHit
from agent_search_gateway.providers.web.brave import BraveAdapter
from tests.support.http import RecordingJsonExecutor

_FIXTURE = Path(__file__).parents[2] / "fixtures/providers/brave/search.json"


async def test_brave_adapter_maps_web_results_as_search_only_provider() -> None:
    executor = RecordingJsonExecutor([json.loads(_FIXTURE.read_text(encoding="utf-8"))])
    adapter = BraveAdapter(
        name="brave",
        api_url="https://brave.example.test/",
        secret=SecretValue("x"),
        http_executor=executor,
    )
    assert await adapter.search("hello world") == [
        KeywordSearchHit("https://example.com/brave", "Brave result", "Brave snippet")
    ]
    request = executor.requests[0]
    assert request.method == "GET"
    assert request.url == "https://brave.example.test/res/v1/web/search?q=hello+world&count=10"
    assert request.headers == {"X-Subscription-Token": "x"}
    assert request.json_body is None
    assert not hasattr(adapter, "fetch")

    empty = BraveAdapter(
        name="brave",
        api_url="https://brave.example.test",
        secret=SecretValue("x"),
        http_executor=RecordingJsonExecutor([{}]),
    )
    assert await empty.search("query") == []

    malformed = BraveAdapter(
        name="brave",
        api_url="https://brave.example.test",
        secret=SecretValue("x"),
        http_executor=RecordingJsonExecutor(
            [{"web": {"results": [{"url": 1, "title": "x", "description": "y"}]}}]
        ),
    )
    with pytest.raises(ExecutionFailure):
        await malformed.search("query")
