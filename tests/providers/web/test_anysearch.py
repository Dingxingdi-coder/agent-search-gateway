import json
from pathlib import Path

import pytest

from agent_search_gateway.errors import ExecutionFailure
from agent_search_gateway.observability import SecretValue
from agent_search_gateway.providers.contracts import KeywordSearchHit
from agent_search_gateway.providers.web.anysearch import AnySearchAdapter
from tests.support.http import RecordingJsonExecutor

_FIXTURE = Path(__file__).parents[2] / "fixtures/providers/anysearch/search.json"


async def test_anysearch_adapter_maps_unified_search_json_as_search_only_provider() -> None:
    executor = RecordingJsonExecutor([json.loads(_FIXTURE.read_text(encoding="utf-8"))])
    adapter = AnySearchAdapter(
        name="anysearch",
        api_url="https://any.example.test/",
        secret=SecretValue("x"),
        http_executor=executor,
    )
    assert await adapter.search("query") == [
        KeywordSearchHit("https://example.com/any", "AnySearch result", "AnySearch snippet")
    ]
    request = executor.requests[0]
    assert request.url == "https://any.example.test/v1/search"
    assert request.headers == {"Authorization": "Bearer x"}
    assert request.json_body == {"query": "query", "format": "json", "max_results": 10}
    assert not hasattr(adapter, "fetch")

    malformed = AnySearchAdapter(
        name="anysearch",
        api_url="https://any.example.test",
        secret=SecretValue("x"),
        http_executor=RecordingJsonExecutor([{"code": 0, "data": {"results": "bad"}}]),
    )
    with pytest.raises(ExecutionFailure):
        await malformed.search("query")

    failed = AnySearchAdapter(
        name="anysearch",
        api_url="https://any.example.test",
        secret=SecretValue("x"),
        http_executor=RecordingJsonExecutor([{"code": -1, "message": "failed"}]),
    )
    with pytest.raises(ExecutionFailure):
        await failed.search("query")
