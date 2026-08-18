from collections.abc import Callable
from typing import Protocol

import pytest

from agent_search_gateway.observability import SecretValue
from agent_search_gateway.providers.contracts import KeywordSearchHit
from agent_search_gateway.providers.web.anysearch import AnySearchAdapter
from agent_search_gateway.providers.web.brave import BraveAdapter
from agent_search_gateway.providers.web.exa import ExaAdapter
from agent_search_gateway.providers.web.firecrawl import FirecrawlAdapter
from agent_search_gateway.providers.web.tavily import TavilyAdapter
from agent_search_gateway.providers.web.tinyfish import TinyFishAdapter
from tests.support.http import RecordingJsonExecutor


class _SearchAdapter(Protocol):
    async def search(self, query: str) -> list[KeywordSearchHit]: ...


AdapterFactory = Callable[[RecordingJsonExecutor], _SearchAdapter]


def _firecrawl(executor: RecordingJsonExecutor) -> _SearchAdapter:
    return FirecrawlAdapter(
        name="firecrawl",
        api_url="https://fire.example.test",
        secret=SecretValue("x"),
        http_executor=executor,
    )


def _tavily(executor: RecordingJsonExecutor) -> _SearchAdapter:
    return TavilyAdapter(
        name="tavily",
        api_url="https://tavily.example.test",
        secret=SecretValue("x"),
        http_executor=executor,
    )


def _exa(executor: RecordingJsonExecutor) -> _SearchAdapter:
    return ExaAdapter(
        name="exa",
        api_url="https://exa.example.test",
        secret=SecretValue("x"),
        http_executor=executor,
    )


def _brave(executor: RecordingJsonExecutor) -> _SearchAdapter:
    return BraveAdapter(
        name="brave",
        api_url="https://brave.example.test",
        secret=SecretValue("x"),
        http_executor=executor,
    )


def _anysearch(executor: RecordingJsonExecutor) -> _SearchAdapter:
    return AnySearchAdapter(
        name="anysearch",
        api_url="https://any.example.test",
        secret=SecretValue("x"),
        http_executor=executor,
    )


def _tinyfish(executor: RecordingJsonExecutor) -> _SearchAdapter:
    return TinyFishAdapter(
        name="tinyfish",
        search_api_url="https://search.tiny.example.test",
        fetch_api_url="https://fetch.tiny.example.test",
        secret=SecretValue("x"),
        http_executor=executor,
    )


@pytest.mark.parametrize(
    ("factory", "payload", "expected_snippet"),
    [
        (
            _firecrawl,
            {
                "success": True,
                "data": {
                    "web": [
                        {"url": "https://example.com/bad", "title": 1},
                        {"url": "https://example.com/good"},
                    ]
                },
            },
            "",
        ),
        (
            _tavily,
            {
                "results": [
                    {"url": "https://example.com/bad", "content": {}},
                    {"url": "https://example.com/good"},
                ]
            },
            "",
        ),
        (
            _exa,
            {
                "results": [
                    {"url": "https://example.com/bad", "title": {}},
                    {
                        "url": "https://example.com/good",
                        "highlights": [""],
                        "summary": "fallback summary",
                    },
                ]
            },
            "fallback summary",
        ),
        (
            _brave,
            {
                "web": {
                    "results": [
                        {"url": "https://example.com/bad", "description": []},
                        {"url": "https://example.com/good"},
                    ]
                }
            },
            "",
        ),
        (
            _anysearch,
            {
                "code": 0,
                "data": {
                    "results": [
                        {"url": "https://example.com/bad", "snippet": []},
                        {"url": "https://example.com/good"},
                    ]
                },
            },
            "",
        ),
        (
            _tinyfish,
            {
                "results": [
                    {"url": "https://example.com/bad", "title": []},
                    {"url": "https://example.com/good"},
                ]
            },
            "",
        ),
    ],
    ids=("firecrawl", "tavily", "exa", "brave", "anysearch", "tinyfish"),
)
async def test_search_adapters_keep_valid_hits_when_presentation_fields_are_malformed(
    factory: AdapterFactory,
    payload: object,
    expected_snippet: str,
) -> None:
    adapter = factory(RecordingJsonExecutor([payload]))

    hits = await adapter.search("query")

    assert len(hits) == 1
    assert hits[0].url == "https://example.com/good"
    assert hits[0].title == ""
    assert hits[0].snippet == expected_snippet
