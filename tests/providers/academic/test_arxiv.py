from datetime import date
from pathlib import Path

import pytest

from agent_search_gateway.errors import ErrorCode, ExecutionFailure, ProtocolFailure
from agent_search_gateway.providers.academic.arxiv import ArxivProvider
from tests.support.http import RecordingJsonExecutor

FIXTURES = Path(__file__).parents[2] / "fixtures" / "providers" / "academic" / "arxiv"


async def test_arxiv_maps_atom_feed_and_isolates_malformed_entry() -> None:
    executor = RecordingJsonExecutor([(FIXTURES / "search.xml").read_text()])
    provider = ArxivProvider(executor)

    hits = await provider.search("graph transformers")

    assert len(hits) == 1
    hit = hits[0]
    assert hit.source == "arxiv"
    assert hit.source_id == "2401.12345"
    assert hit.arxiv_id == "2401.12345"
    assert hit.title == "Example arXiv Paper"
    assert hit.authors == ("Alice Example", "Bob Example")
    assert hit.abstract == "Example abstract."
    assert hit.doi == "10.1000/arxiv.example"
    assert hit.published_date == date(2024, 1, 2)
    assert hit.updated_date == date(2024, 2, 3)
    assert hit.topics == ("cs.AI", "cs.LG")
    assert hit.url == "https://arxiv.org/abs/2401.12345v2"
    assert hit.pdf_url == "https://arxiv.org/pdf/2401.12345v2.pdf"

    request = executor.requests[0]
    assert request.method == "GET"
    assert request.response_mode == "text"
    assert request.headers is None
    assert request.params == {
        "search_query": "all:graph transformers",
        "max_results": 10,
        "sortBy": "relevance",
        "sortOrder": "descending",
    }


async def test_arxiv_malformed_xml_envelope_is_protocol_failure() -> None:
    executor = RecordingJsonExecutor([(FIXTURES / "malformed.xml").read_text()])
    with pytest.raises(ProtocolFailure) as caught:
        await ArxivProvider(executor).search("query")
    assert caught.value.code is ErrorCode.PROTOCOL_ERROR


async def test_arxiv_http_failure_propagates_without_adapter_retry() -> None:
    failure = ExecutionFailure(ErrorCode.ALL_PROVIDERS_FAILED, "transport")
    executor = RecordingJsonExecutor([failure])
    with pytest.raises(ExecutionFailure):
        await ArxivProvider(executor).search("query")
    assert len(executor.requests) == 1
