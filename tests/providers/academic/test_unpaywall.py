import json
from pathlib import Path

import pytest

from agent_search_gateway.errors import ErrorCode, ExecutionFailure, ProtocolFailure
from agent_search_gateway.observability import SecretValue
from agent_search_gateway.providers.academic.unpaywall import UnpaywallResolver
from agent_search_gateway.providers.http import HttpStatusFailure
from tests.support.http import RecordingJsonExecutor

FIXTURES = Path(__file__).parents[2] / "fixtures" / "providers" / "academic" / "unpaywall"


def _fixture(name: str) -> object:
    return json.loads((FIXTURES / name).read_text())


async def test_unpaywall_maps_best_pdf_and_contact_param() -> None:
    executor = RecordingJsonExecutor([_fixture("oa.json")])
    resolver = UnpaywallResolver(
        executor,
        contact_email=SecretValue("[REDACTED_SECRET]"),
    )
    resolved = await resolver.resolve("https://doi.org/10.1000/UNPAYWALL.EXAMPLE")

    assert resolved is not None
    assert str(resolved.landing_url) == "https://repository.example/article"
    assert str(resolved.pdf_url) == "https://repository.example/article.pdf"
    assert resolved.is_open_access is True
    assert resolved.oa_status == "gold"
    assert resolved.license == "cc-by"
    request = executor.requests[0]
    assert request.url == "https://api.unpaywall.org/v2/10.1000%2Funpaywall.example"
    assert request.params == {"email": "[REDACTED_SECRET]"}


async def test_unpaywall_percent_encodes_reserved_doi_path_characters() -> None:
    executor = RecordingJsonExecutor([_fixture("non_oa.json")])
    resolver = UnpaywallResolver(
        executor,
        contact_email=SecretValue("[REDACTED_SECRET]"),
    )

    await resolver.resolve("10.1000/example#part?query")

    request = executor.requests[0]
    assert request.url == "https://api.unpaywall.org/v2/10.1000%2Fexample%23part%3Fquery"
    assert request.params == {"email": "[REDACTED_SECRET]"}


async def test_unpaywall_handles_landing_only_and_deterministic_alternate_fallback() -> None:
    landing_only = {
        "is_oa": True,
        "oa_status": "green",
        "best_oa_location": {
            "url_for_landing_page": "https://best.example/landing",
            "url_for_pdf": None,
            "license": "cc-by",
        },
        "oa_locations": [],
    }
    alternate = {
        "is_oa": True,
        "oa_status": "green",
        "best_oa_location": None,
        "oa_locations": [
            {"url_for_pdf": "https://z.example/paper.pdf", "url": "https://z.example"},
            {"url_for_pdf": "https://a.example/paper.pdf", "url": "https://a.example"},
        ],
    }
    executor = RecordingJsonExecutor([landing_only, alternate])
    resolver = UnpaywallResolver(
        executor,
        contact_email=SecretValue("[REDACTED_SECRET]"),
    )
    first = await resolver.resolve("10.1000/landing")
    second = await resolver.resolve("10.1000/alternate")
    assert first is not None and first.pdf_url is None
    assert str(first.landing_url) == "https://best.example/landing"
    assert second is not None
    assert str(second.pdf_url) == "https://a.example/paper.pdf"
    assert str(second.landing_url) == "https://a.example"


async def test_unpaywall_skips_invalid_best_location_for_valid_alternate() -> None:
    payload = {
        "is_oa": True,
        "oa_status": "green",
        "best_oa_location": {
            "url_for_landing_page": "not-a-url",
            "url_for_pdf": "ftp://invalid.example/paper.pdf",
        },
        "oa_locations": [
            {
                "url_for_landing_page": "https://valid.example/landing",
                "url_for_pdf": "https://valid.example/paper.pdf",
                "license": "cc-by",
            }
        ],
    }
    executor = RecordingJsonExecutor([payload])
    resolver = UnpaywallResolver(
        executor,
        contact_email=SecretValue("[REDACTED_SECRET]"),
    )

    resolved = await resolver.resolve("10.1000/alternate-valid")

    assert resolved is not None
    assert str(resolved.landing_url) == "https://valid.example/landing"
    assert str(resolved.pdf_url) == "https://valid.example/paper.pdf"
    assert resolved.license == "cc-by"


async def test_unpaywall_non_oa_and_404_are_normal_results() -> None:
    executor = RecordingJsonExecutor(
        [
            _fixture("non_oa.json"),
            HttpStatusFailure("unpaywall", "oa_resolve", 404),
        ]
    )
    resolver = UnpaywallResolver(
        executor,
        contact_email=SecretValue("[REDACTED_SECRET]"),
    )
    closed = await resolver.resolve("10.1000/closed.example")
    assert closed is not None
    assert closed.is_open_access is False
    assert closed.landing_url is None
    assert closed.pdf_url is None
    assert await resolver.resolve("10.1000/missing") is None


async def test_unpaywall_non_404_and_protocol_failures_propagate() -> None:
    executor = RecordingJsonExecutor(
        [
            HttpStatusFailure("unpaywall", "oa_resolve", 503),
            _fixture("malformed.json"),
        ]
    )
    resolver = UnpaywallResolver(
        executor,
        contact_email=SecretValue("[REDACTED_SECRET]"),
    )
    with pytest.raises(ExecutionFailure):
        await resolver.resolve("10.1000/status")
    with pytest.raises(ProtocolFailure) as caught:
        await resolver.resolve("10.1000/malformed")
    assert caught.value.code is ErrorCode.PROTOCOL_ERROR
