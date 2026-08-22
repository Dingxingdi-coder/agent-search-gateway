from datetime import date
from pathlib import Path

import pytest

from agent_search_gateway.errors import ErrorCode, ExecutionFailure, ProtocolFailure
from agent_search_gateway.providers.academic.dblp import DblpProvider
from tests.support.http import RecordingJsonExecutor

FIXTURES = Path(__file__).parents[2] / "fixtures" / "providers" / "academic" / "dblp"


async def test_dblp_maps_publication_xml_and_uses_stable_key() -> None:
    executor = RecordingJsonExecutor([(FIXTURES / "search.xml").read_text()])
    hits = await DblpProvider(executor).search("structured search")

    assert len(hits) == 1
    hit = hits[0]
    assert hit.source_id == "conf/example/Author24"
    assert hit.title == "A Structured DBLP Paper"
    assert hit.authors == ("Alice Example", "Bob Example")
    assert hit.venue == "ExampleConf"
    assert hit.published_date == date(2024, 1, 1)
    assert hit.url == "https://dblp.org/rec/conf/example/Author24"
    assert hit.doi == "10.1000/dblp.example"
    assert hit.abstract == ""

    request = executor.requests[0]
    assert request.method == "GET"
    assert request.url == "https://dblp.org/search/publ/api"
    assert request.response_mode == "text"
    assert request.params == {"q": "structured search", "format": "xml", "h": 10}


async def test_dblp_malformed_xml_is_protocol_failure() -> None:
    executor = RecordingJsonExecutor([(FIXTURES / "malformed.xml").read_text()])
    with pytest.raises(ProtocolFailure) as caught:
        await DblpProvider(executor).search("query")
    assert caught.value.code is ErrorCode.PROTOCOL_ERROR


async def test_dblp_failure_does_not_trigger_html_fallback() -> None:
    executor = RecordingJsonExecutor(
        [ExecutionFailure(ErrorCode.ALL_PROVIDERS_FAILED, "failed")]
    )
    with pytest.raises(ExecutionFailure):
        await DblpProvider(executor).search("query")
    assert len(executor.requests) == 1
