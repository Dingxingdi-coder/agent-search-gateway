import json
from datetime import date
from pathlib import Path

import pytest

from agent_search_gateway.errors import ErrorCode, ProtocolFailure
from agent_search_gateway.observability import SecretValue
from agent_search_gateway.providers.academic.crossref import CrossrefProvider
from tests.support.http import RecordingJsonExecutor

FIXTURES = Path(__file__).parents[2] / "fixtures" / "providers" / "academic" / "crossref"


def _fixture(name: str) -> object:
    return json.loads((FIXTURES / name).read_text())


async def test_crossref_maps_metadata_and_never_invents_missing_date() -> None:
    executor = RecordingJsonExecutor([_fixture("search.json")])
    hits = await CrossrefProvider(executor).search("metadata")

    assert len(hits) == 2
    hit = hits[0]
    assert hit.source_id == "10.1000/crossref.example"
    assert hit.doi == "10.1000/crossref.example"
    assert hit.title == "Crossref Example"
    assert hit.authors == ("Alice Example", "Consortium")
    assert hit.abstract == "A structured abstract."
    assert hit.venue == "Example Journal"
    assert hit.published_date == date(2023, 11, 9)
    assert hit.citation_count == 42
    assert hit.url == "https://publisher.example/crossref"
    assert hit.pdf_url == "https://publisher.example/crossref.pdf"
    assert hits[1].published_date is None
    assert hits[1].url == "https://doi.org/10.1000/no.date"

    request = executor.requests[0]
    assert request.method == "GET"
    assert request.url == "https://api.crossref.org/works"
    assert request.params == {
        "query": "metadata",
        "rows": 10,
        "sort": "relevance",
        "order": "desc",
    }


def test_crossref_date_defaults_boolean_month_and_day() -> None:
    assert CrossrefProvider._date({"published": {"date-parts": [[2024, True, False]]}}) == date(
        2024,
        1,
        1,
    )


async def test_crossref_optional_contact_adds_mailto_only_when_configured() -> None:
    executor = RecordingJsonExecutor([{"message": {"items": []}}])
    provider = CrossrefProvider(
        executor,
        contact_email=SecretValue("[REDACTED_SECRET]"),
    )
    await provider.search("query")
    assert executor.requests[0].params == {
        "query": "query",
        "rows": 10,
        "sort": "relevance",
        "order": "desc",
        "mailto": "[REDACTED_SECRET]",
    }


async def test_crossref_invalid_items_envelope_is_protocol_failure() -> None:
    executor = RecordingJsonExecutor([_fixture("malformed.json")])
    with pytest.raises(ProtocolFailure) as caught:
        await CrossrefProvider(executor).search("query")
    assert caught.value.code is ErrorCode.PROTOCOL_ERROR
