import json
from datetime import date
from pathlib import Path

import pytest

from agent_search_gateway.errors import ErrorCode, ProtocolFailure
from agent_search_gateway.observability import SecretValue
from agent_search_gateway.providers.academic.openalex import (
    OpenAlexProvider,
    reconstruct_abstract,
)
from tests.support.http import RecordingJsonExecutor

FIXTURES = Path(__file__).parents[2] / "fixtures" / "providers" / "academic" / "openalex"


def _fixture(name: str) -> object:
    return json.loads((FIXTURES / name).read_text())


def test_reconstruct_abstract_sorts_positions_and_accepts_empty_index() -> None:
    assert reconstruct_abstract({"world": [1], "Hello": [0], "again": [2]}) == "Hello world again"
    assert reconstruct_abstract({}) == ""
    assert reconstruct_abstract(None) == ""


async def test_openalex_maps_works_and_falls_back_to_canonical_landing() -> None:
    executor = RecordingJsonExecutor([_fixture("search.json")])
    hits = await OpenAlexProvider(executor).search("open science")

    assert len(hits) == 2
    hit = hits[0]
    assert hit.source_id == "W2741809807"
    assert hit.doi == "https://doi.org/10.1000/OPENALEX.EXAMPLE"
    assert hit.authors == ("Alice Example", "Bob Example")
    assert hit.abstract == "Hello world again"
    assert hit.published_date == date(2024, 3, 4)
    assert hit.url == "https://publisher.example/paper"
    assert hit.pdf_url == "https://publisher.example/paper.pdf"
    assert hit.citation_count == 77
    assert hit.is_open_access is True
    assert hit.oa_status == "gold"
    assert hit.license == "cc-by"
    assert hit.topics == ("Machine Learning", "Artificial Intelligence")
    assert hits[1].url == "https://openalex.org/W999"
    assert hits[1].is_open_access is False

    request = executor.requests[0]
    assert request.method == "GET"
    assert request.url == "https://api.openalex.org/works"
    assert request.params == {"search": "open science", "per_page": 10}


async def test_openalex_optional_contact_is_request_identity_only() -> None:
    executor = RecordingJsonExecutor([{"results": []}])
    provider = OpenAlexProvider(
        executor,
        contact_email=SecretValue("[REDACTED_SECRET]"),
    )
    await provider.search("query")
    assert executor.requests[0].params == {
        "search": "query",
        "per_page": 10,
        "mailto": "[REDACTED_SECRET]",
    }


async def test_openalex_invalid_results_envelope_is_protocol_failure() -> None:
    executor = RecordingJsonExecutor([_fixture("malformed.json")])
    with pytest.raises(ProtocolFailure) as caught:
        await OpenAlexProvider(executor).search("query")
    assert caught.value.code is ErrorCode.PROTOCOL_ERROR
