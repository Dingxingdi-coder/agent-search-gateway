import io
from pathlib import Path

from agent_search_gateway.academic.aggregator import PaperAggregator
from agent_search_gateway.concurrency import ProviderQuotaManager
from agent_search_gateway.errors import ErrorCode, ExecutionFailure
from agent_search_gateway.observability import configure_debug_logging
from agent_search_gateway.orchestrators.paper import PaperSearchOrchestrator
from agent_search_gateway.providers.contracts import PaperSearchHit
from agent_search_gateway.result_writer import ResultWriter
from agent_search_gateway.url_store import URLStore
from tests.support.fakes import FakeAcademicSearchProvider, FakeOAResolver


async def test_paper_workflow_logs_lifecycle_rejection_and_enrichment_without_payloads(
    tmp_path: Path,
) -> None:
    query = "QUERY_PAYLOAD_SENTINEL"
    title = "TITLE_PAYLOAD_SENTINEL"
    abstract = "ABSTRACT_PAYLOAD_SENTINEL"
    doi = "10.1000/doi-payload-sentinel"
    valid = PaperSearchHit(
        source="openalex",
        source_id="W123",
        title=title,
        authors=("A. Author",),
        abstract=abstract,
        doi=doi,
        url="https://example.test/paper",
    )
    invalid = PaperSearchHit(
        source="openalex",
        source_id="",
        title="INVALID_TITLE_SENTINEL",
        url="https://example.test/invalid",
    )
    good = FakeAcademicSearchProvider("openalex", [valid, invalid])
    failed = FakeAcademicSearchProvider(
        "core",
        failure=ExecutionFailure(ErrorCode.ALL_PROVIDERS_FAILED, "provider detail sentinel"),
    )
    resolver = FakeOAResolver(
        failure=ExecutionFailure(ErrorCode.ALL_PROVIDERS_FAILED, "resolver detail sentinel")
    )
    log_file = tmp_path / "debug.log"
    session = configure_debug_logging(log_file, stderr=io.StringIO())
    try:
        orchestrator = PaperSearchOrchestrator(
            providers=(good, failed),
            quotas=ProviderQuotaManager(
                web_limits={},
                llm_limits={},
                academic_limits={"openalex": 1, "core": 1},
            ),
            aggregator=PaperAggregator(("openalex", "core")),
            resolver=resolver,
            store=URLStore(),
            result_writer=ResultWriter(tmp_path / "results"),
        )
        await orchestrator.paper_search(query, request_id="11111111")
    finally:
        session.close()

    text = log_file.read_text(encoding="utf-8")
    assert "event=provider_started" in text
    assert "event=provider_completed" in text
    assert "event=provider_failed" in text
    assert "stage=paper_search" in text
    assert "event=paper_candidate_rejected" in text
    assert "reason=missing_source_id" in text
    assert "event=paper_enrichment_failed" in text
    assert "stage=oa_resolve" in text
    for sentinel in (
        query,
        title,
        abstract,
        doi,
        "INVALID_TITLE_SENTINEL",
        "provider detail sentinel",
        "resolver detail sentinel",
    ):
        assert sentinel not in text


def test_transitive_cluster_merge_log_contains_reason_but_no_identifiers(tmp_path: Path) -> None:
    doi = "10.1000/bridge-log-sentinel"
    arxiv_id = "2401.98765"
    log_file = tmp_path / "debug.log"
    session = configure_debug_logging(log_file, stderr=io.StringIO())
    try:
        PaperAggregator(("crossref", "arxiv", "semantic_scholar")).aggregate(
            [
                PaperSearchHit(
                    source="crossref",
                    source_id=doi,
                    title="DOI paper",
                    doi=doi,
                    url="https://doi.org/10.1000/bridge-log-sentinel",
                ),
                PaperSearchHit(
                    source="arxiv",
                    source_id=arxiv_id,
                    title="arXiv paper",
                    arxiv_id=arxiv_id,
                    url="https://arxiv.org/abs/2401.98765",
                ),
                PaperSearchHit(
                    source="semantic_scholar",
                    source_id="ABCDEF9876",
                    title="Bridge paper",
                    doi=doi,
                    arxiv_id=arxiv_id,
                    url="https://example.test/bridge",
                ),
            ]
        )
    finally:
        session.close()

    text = log_file.read_text(encoding="utf-8")
    assert "event=paper_clusters_merged" in text
    assert "reason=bridged_identity" in text
    assert "clusters=2" in text
    assert doi not in text
    assert arxiv_id not in text
