import logging

from agent_search_gateway.academic.aggregator import PaperAggregator
from agent_search_gateway.academic.enrichment import enrich_paper_records
from agent_search_gateway.errors import ErrorCode, ExecutionFailure
from agent_search_gateway.models import OAResolution, PaperIdentifiers, PaperRecord
from agent_search_gateway.providers.contracts import PaperSearchHit
from agent_search_gateway.url_normalization import normalize_url


class RecordingResolver:
    name = "unpaywall"

    def __init__(
        self,
        response: OAResolution | None = None,
        failure: Exception | None = None,
    ) -> None:
        self.response = response
        self.failure = failure
        self.calls: list[str] = []

    async def resolve(self, doi: str) -> OAResolution | None:
        self.calls.append(doi)
        if self.failure is not None:
            raise self.failure
        return self.response


def _record(*, doi: str = "10.1000/example", pdf: str | None = None) -> PaperRecord:
    return PaperRecord(
        title="Paper",
        authors=("Author",),
        abstract="Abstract",
        identifiers=PaperIdentifiers(doi=doi),
        published_date=None,
        updated_date=None,
        url=normalize_url("https://publisher.example/paper"),
        pdf_url=normalize_url(pdf) if pdf is not None else None,
        venue="",
        topics=(),
        citation_counts={},
        is_open_access=None,
        oa_status="",
        license="",
        sources=("openalex",),
    )


async def test_enrichment_runs_once_after_six_source_hits_deduplicate() -> None:
    providers = ("arxiv", "semantic_scholar", "openalex", "dblp", "crossref", "core")
    hits = [
        PaperSearchHit(
            source=source,
            source_id=(
                "2401.12345"
                if source == "arxiv"
                else "10.1000/shared"
                if source == "crossref"
                else f"id-{index}"
            ),
            title="Shared Paper",
            authors=("Author",),
            doi="10.1000/shared",
            arxiv_id="2401.12345" if source == "arxiv" else "",
            url=f"https://example.test/{source}",
        )
        for index, source in enumerate(providers)
    ]
    records = PaperAggregator(providers).aggregate(hits)
    resolver = RecordingResolver(
        OAResolution(
            landing_url=normalize_url("https://repository.example/paper"),
            pdf_url=normalize_url("https://repository.example/paper.pdf"),
            is_open_access=True,
            oa_status="green",
            license="cc-by",
        )
    )

    enriched = await enrich_paper_records(records, resolver)

    assert len(records) == 1
    assert resolver.calls == ["10.1000/shared"]
    assert str(enriched[0].pdf_url) == "https://repository.example/paper.pdf"
    assert enriched[0].is_open_access is True
    assert enriched[0].oa_status == "green"
    assert enriched[0].license == "cc-by"


async def test_enrichment_skips_doi_less_and_keeps_stronger_direct_pdf() -> None:
    resolver = RecordingResolver(
        OAResolution(
            landing_url=normalize_url("https://repository.example/paper"),
            pdf_url=normalize_url("https://repository.example/weaker.pdf"),
            is_open_access=True,
            oa_status="green",
            license="cc-by",
        )
    )
    direct = _record(pdf="https://publisher.example/direct.pdf")
    doi_less = _record(doi="")

    enriched = await enrich_paper_records([direct, doi_less], resolver)

    assert resolver.calls == ["10.1000/example"]
    assert str(enriched[0].pdf_url) == "https://publisher.example/direct.pdf"
    assert enriched[1] == doi_less


async def test_enrichment_failure_logs_safe_event_and_retains_record(caplog: object) -> None:
    resolver = RecordingResolver(
        failure=ExecutionFailure(ErrorCode.ALL_PROVIDERS_FAILED, "resolver failed")
    )
    original = _record()
    with caplog.at_level(logging.DEBUG):  # type: ignore[attr-defined]
        enriched = await enrich_paper_records([original], resolver)
    assert enriched == [original]
    events = [getattr(record, "gateway_event", "") for record in caplog.records]  # type: ignore[attr-defined]
    assert "paper_enrichment_failed" in events
    rendered_fields = repr(
        [getattr(record, "gateway_fields", {}) for record in caplog.records]  # type: ignore[attr-defined]
    )
    assert "10.1000/example" not in rendered_fields
