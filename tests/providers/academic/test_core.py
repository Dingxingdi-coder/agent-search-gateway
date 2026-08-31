import json
from datetime import date
from pathlib import Path

import pytest

from agent_search_gateway.errors import ErrorCode, ExecutionFailure, ProtocolFailure
from agent_search_gateway.observability import SecretValue
from agent_search_gateway.providers.academic.core import CoreProvider
from tests.support.http import RecordingJsonExecutor

FIXTURES = Path(__file__).parents[2] / "fixtures" / "providers" / "academic" / "core"


def _fixture(name: str) -> object:
    return json.loads((FIXTURES / name).read_text())


async def test_core_maps_works_with_required_bearer_auth() -> None:
    executor = RecordingJsonExecutor([_fixture("search.json")])
    hits = await CoreProvider(
        executor,
        api_key=SecretValue("[REDACTED_SECRET]"),
    ).search("repositories")

    assert len(hits) == 2
    hit = hits[0]
    assert hit.source_id == "123456"
    assert hit.title == "CORE Example"
    assert hit.authors == ("Alice Example", "Bob Example")
    assert hit.abstract == "Repository abstract."
    assert hit.doi == "10.1000/CORE.EXAMPLE"
    assert hit.published_date == date(2022, 5, 6)
    assert hit.url == "https://core.ac.uk/works/123456"
    assert hit.pdf_url == "https://core.ac.uk/download/pdf/123456.pdf"
    assert hit.venue == "Example Repository"
    assert hit.topics == ("Machine Learning", "Open Access")
    assert hit.citation_count == 8
    assert hits[1].authors == ("String Author",)
    assert hits[1].pdf_url == "https://repository.example/fallback.pdf"

    request = executor.requests[0]
    assert request.method == "GET"
    assert request.url == "https://api.core.ac.uk/v3/search/works/"
    assert request.headers == {"Authorization": "Bearer [REDACTED_SECRET]"}
    assert request.params == {"q": "repositories", "limit": 10, "offset": 0}


async def test_core_bearer_is_sent_without_authentication_fallback() -> None:
    executor = RecordingJsonExecutor([{"results": []}])
    provider = CoreProvider(executor, api_key=SecretValue("[REDACTED_SECRET]"))
    await provider.search("query")
    assert executor.requests[0].headers == {"Authorization": "Bearer [REDACTED_SECRET]"}

    failed = RecordingJsonExecutor(
        [ExecutionFailure(ErrorCode.ALL_PROVIDERS_FAILED, "authentication rejected")]
    )
    with pytest.raises(ExecutionFailure):
        await CoreProvider(failed, api_key=SecretValue("[REDACTED_SECRET]")).search("query")
    assert len(failed.requests) == 1


async def test_core_invalid_results_envelope_is_protocol_failure() -> None:
    executor = RecordingJsonExecutor([_fixture("malformed.json")])
    with pytest.raises(ProtocolFailure) as caught:
        await CoreProvider(executor, api_key=SecretValue("[REDACTED_SECRET]")).search(
            "query"
        )
    assert caught.value.code is ErrorCode.PROTOCOL_ERROR
