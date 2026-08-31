import json
from datetime import date
from pathlib import Path

import pytest

from agent_search_gateway.errors import ErrorCode, ExecutionFailure, ProtocolFailure
from agent_search_gateway.observability import SecretValue
from agent_search_gateway.providers.academic.semantic_scholar import SemanticScholarProvider
from tests.support.http import RecordingJsonExecutor

FIXTURES = Path(__file__).parents[2] / "fixtures" / "providers" / "academic" / "semantic_scholar"


def _fixture(name: str) -> object:
    return json.loads((FIXTURES / name).read_text())


async def test_semantic_scholar_maps_graph_search_without_auth_when_omitted() -> None:
    executor = RecordingJsonExecutor([_fixture("search.json")])
    hits = await SemanticScholarProvider(executor).search("transformers")

    assert len(hits) == 2
    hit = hits[0]
    assert hit.source_id == "649DEF34F8BE52C8B66281AF98AE884C09AEF38B"
    assert hit.doi == "10.5555/3295222.3295349"
    assert hit.arxiv_id == "1706.03762v5"
    assert hit.authors == ("Ashish Vaswani", "Noam Shazeer")
    assert hit.published_date == date(2017, 6, 12)
    assert hit.citation_count == 12345
    assert hit.topics == ("Computer Science", "Mathematics")
    assert hit.pdf_url == "https://example.test/attention.pdf"
    assert hit.is_open_access is True
    assert hits[1].abstract == ""
    assert hits[1].pdf_url == ""

    request = executor.requests[0]
    assert request.method == "GET"
    assert request.url.endswith("/paper/search")
    assert request.headers is None
    assert request.params == {
        "query": "transformers",
        "limit": 10,
        "fields": (
            "title,abstract,citationCount,authors,url,publicationDate,"
            "externalIds,fieldsOfStudy,openAccessPdf"
        ),
    }


async def test_semantic_scholar_optional_auth_header_is_sent_once() -> None:
    executor = RecordingJsonExecutor([{"data": []}])
    await SemanticScholarProvider(
        executor,
        api_key=SecretValue("[REDACTED_SECRET]"),
    ).search("query")
    assert executor.requests[0].headers == {"x-api-key": "[REDACTED_SECRET]"}


async def test_semantic_scholar_invalid_envelope_is_protocol_failure() -> None:
    executor = RecordingJsonExecutor([_fixture("malformed.json")])
    with pytest.raises(ProtocolFailure) as caught:
        await SemanticScholarProvider(executor).search("query")
    assert caught.value.code is ErrorCode.PROTOCOL_ERROR
    assert caught.value.reason == "invalid_data_envelope"


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ({}, "missing_data_envelope"),
        ({"error": "RAW_PROVIDER_DETAIL_SENTINEL"}, "missing_data_envelope"),
        ({"data": None}, "invalid_data_envelope"),
        ([], "invalid_data_envelope"),
    ],
)
async def test_semantic_scholar_classifies_unexpected_success_envelopes(
    payload: object,
    reason: str,
) -> None:
    with pytest.raises(ProtocolFailure) as caught:
        await SemanticScholarProvider(RecordingJsonExecutor([payload])).search("query")

    assert caught.value.reason == reason
    assert reason in caught.value.message
    assert "RAW_PROVIDER_DETAIL_SENTINEL" not in caught.value.message


async def test_semantic_scholar_auth_failure_has_no_unauthenticated_fallback() -> None:
    executor = RecordingJsonExecutor(
        [ExecutionFailure(ErrorCode.ALL_PROVIDERS_FAILED, "authentication rejected")]
    )
    provider = SemanticScholarProvider(
        executor,
        api_key=SecretValue("[REDACTED_SECRET]"),
    )
    with pytest.raises(ExecutionFailure):
        await provider.search("query")
    assert len(executor.requests) == 1
