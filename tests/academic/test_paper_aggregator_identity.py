from datetime import date
from itertools import permutations

from agent_search_gateway.academic.aggregator import PaperAggregator
from agent_search_gateway.providers.contracts import PaperSearchHit

PRIORITY = ("arxiv", "semantic_scholar", "openalex", "dblp", "crossref", "core")


def _hit(
    source: str,
    source_id: str,
    *,
    title: str = "Paper",
    doi: str = "",
    arxiv_id: str = "",
    authors: tuple[str, ...] = ("A. Author",),
    published_date: date | None = date(2024, 1, 1),
    url: str | None = None,
) -> PaperSearchHit:
    return PaperSearchHit(
        source=source,
        source_id=source_id,
        title=title,
        doi=doi,
        arxiv_id=arxiv_id,
        authors=authors,
        published_date=published_date,
        url=url or f"https://example.test/{source}/{source_id}",
    )


def test_same_strong_identifiers_merge_directly() -> None:
    aggregator = PaperAggregator(PRIORITY)
    same_doi = aggregator.aggregate(
        [
            _hit("openalex", "W1", doi="10.1000/example"),
            _hit("semantic_scholar", "ABCDEF12", doi="https://doi.org/10.1000/EXAMPLE"),
        ]
    )
    assert len(same_doi) == 1

    same_arxiv = aggregator.aggregate(
        [
            _hit("arxiv", "2401.12345v1", arxiv_id="2401.12345v1"),
            _hit("semantic_scholar", "ABCDEF13", arxiv_id="arXiv:2401.12345v4"),
        ]
    )
    assert len(same_arxiv) == 1

    same_native = aggregator.aggregate(
        [
            _hit("openalex", "W2", title="First"),
            _hit("openalex", "https://openalex.org/W2", title="Second"),
        ]
    )
    assert len(same_native) == 1


def test_doi_arxiv_bridge_merges_transitively_for_representative_permutations() -> None:
    candidates = (
        _hit("crossref", "10.1000/bridge", doi="10.1000/bridge", title="DOI only"),
        _hit("arxiv", "2401.22222", arxiv_id="2401.22222", title="arXiv only"),
        _hit(
            "semantic_scholar",
            "ABCDEF14",
            doi="10.1000/bridge",
            arxiv_id="2401.22222v3",
            title="Bridge",
        ),
    )
    aggregator = PaperAggregator(PRIORITY)
    for ordered in permutations(candidates):
        records = aggregator.aggregate(list(ordered))
        assert len(records) == 1
        assert records[0].identifiers.doi == "10.1000/bridge"
        assert records[0].identifiers.arxiv_id == "2401.22222"


def test_conflicting_doi_for_same_native_identity_rejects_incoming_candidate() -> None:
    aggregator = PaperAggregator(PRIORITY)
    records = aggregator.aggregate(
        [
            _hit("openalex", "W3", doi="10.1000/established", title="Established"),
            _hit("openalex", "W3", doi="10.1000/conflict", title="Incoming"),
        ]
    )
    assert len(records) == 1
    assert records[0].identifiers.doi == "10.1000/established"
    assert records[0].title == "Established"


def test_different_explicit_dois_never_weak_merge() -> None:
    records = PaperAggregator(PRIORITY).aggregate(
        [
            _hit("openalex", "W4", doi="10.1000/a"),
            _hit("semantic_scholar", "ABCDEF15", doi="10.1000/b"),
        ]
    )
    assert len(records) == 2


def test_weak_identity_is_conservative_and_requires_title_author_and_year() -> None:
    aggregator = PaperAggregator(PRIORITY)
    weak_pair = [
        _hit("openalex", "W5", title="  Same   Paper ", doi=""),
        _hit("core", "core-5", title="same paper", doi=""),
    ]
    assert len(aggregator.aggregate(weak_pair)) == 1

    missing_year = [weak_pair[0], _hit("core", "core-6", title="same paper", published_date=None)]
    incompatible_year = [
        weak_pair[0],
        _hit("core", "core-7", title="same paper", published_date=date(2025, 1, 1)),
    ]
    incompatible_authors = [
        weak_pair[0],
        _hit("core", "core-8", title="same paper", authors=("Different",)),
    ]
    similar_title = [
        weak_pair[0],
        _hit("core", "core-9", title="Same Paper: Survey"),
    ]
    assert len(aggregator.aggregate(missing_year)) == 2
    assert len(aggregator.aggregate(incompatible_year)) == 2
    assert len(aggregator.aggregate(incompatible_authors)) == 2
    assert len(aggregator.aggregate(similar_title)) == 2


def test_cluster_order_follows_provider_priority_not_input_completion_order() -> None:
    records = PaperAggregator(PRIORITY).aggregate(
        [
            _hit("core", "core-z", title="Core"),
            _hit("arxiv", "2401.33333", title="arXiv", arxiv_id="2401.33333"),
            _hit("openalex", "W9", title="OpenAlex"),
        ]
    )
    assert [record.title for record in records] == ["arXiv", "OpenAlex", "Core"]
