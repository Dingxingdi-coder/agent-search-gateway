from dataclasses import FrozenInstanceError
from datetime import date

import pytest

from agent_search_gateway.academic.normalization import (
    bibliographic_fingerprint,
    bibliographic_fingerprints_match,
    normalize_arxiv_id,
    normalize_core_id,
    normalize_dblp_key,
    normalize_doi,
    normalize_openalex_id,
    normalize_semantic_scholar_id,
    source_identity_key,
)
from agent_search_gateway.models import OAResolution, PaperIdentifiers, PaperRecord
from agent_search_gateway.providers.contracts import (
    AcademicSearchProvider,
    OAResolver,
    PaperSearchHit,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("10.1000/ABC", "10.1000/abc"),
        (" DOI: 10.1000/ABC ", "10.1000/abc"),
        ("https://doi.org/10.1000/ABC", "10.1000/abc"),
        ("http://dx.doi.org/10.1000%2FABC", "10.1000/abc"),
    ],
)
def test_normalize_doi_accepts_equivalent_forms(value: str, expected: str) -> None:
    assert normalize_doi(value) == expected
    assert normalize_doi(expected) == expected


@pytest.mark.parametrize("value", ["", "not-a-doi", "https://example.com/10.1000/abc", "10.12/x"])
def test_normalize_doi_rejects_invalid_identifiers(value: str) -> None:
    assert normalize_doi(value) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2401.12345", "2401.12345"),
        ("2401.12345v2", "2401.12345"),
        ("arXiv:2401.12345v3", "2401.12345"),
        ("https://arxiv.org/abs/2401.12345v4", "2401.12345"),
        ("https://arxiv.org/pdf/2401.12345v5.pdf", "2401.12345"),
    ],
)
def test_normalize_arxiv_id_strips_version(value: str, expected: str) -> None:
    assert normalize_arxiv_id(value) == expected


def test_normalize_arxiv_id_rejects_legacy_and_unrelated_forms() -> None:
    assert normalize_arxiv_id("hep-th/9901001") is None
    assert normalize_arxiv_id("https://example.com/abs/2401.12345") is None


def test_provider_native_ids_are_canonical_and_source_namespaced() -> None:
    assert normalize_semantic_scholar_id(" 649DEF34F8BE52C8B66281AF98AE884C09AEF38B ") == (
        "649DEF34F8BE52C8B66281AF98AE884C09AEF38B"
    )
    assert normalize_openalex_id("https://openalex.org/w2741809807") == "W2741809807"
    assert normalize_dblp_key(
        "https://dblp.org/rec/conf/nips/VaswaniSPUJGKP17.html"
    ) == (
        "conf/nips/VaswaniSPUJGKP17"
    )
    assert normalize_core_id(" 123456789 ") == "123456789"
    assert source_identity_key("openalex", "W123") == "openalex:W123"
    assert source_identity_key("core", "W123") == "core:W123"


def test_bibliographic_fingerprint_requires_exact_title_author_overlap_and_same_known_year(
) -> None:
    left = bibliographic_fingerprint(
        "  Attention   Is All You Need ",
        ("Ashish Vaswani", "Noam Shazeer"),
        date(2017, 6, 12),
    )
    equivalent = bibliographic_fingerprint(
        "attention is all you need",
        ("Ashish   Vaswani", "Other Author"),
        date(2017, 1, 1),
    )
    assert bibliographic_fingerprints_match(left, equivalent)

    similar_title = bibliographic_fingerprint(
        "Attention Is All You Need: A Survey",
        ("Ashish Vaswani",),
        date(2017, 1, 1),
    )
    incompatible_authors = bibliographic_fingerprint(
        "Attention Is All You Need",
        ("Different Author",),
        date(2017, 1, 1),
    )
    incompatible_year = bibliographic_fingerprint(
        "Attention Is All You Need",
        ("Ashish Vaswani",),
        date(2018, 1, 1),
    )
    missing_year = bibliographic_fingerprint(
        "Attention Is All You Need",
        ("Ashish Vaswani",),
        None,
    )
    assert not bibliographic_fingerprints_match(left, similar_title)
    assert not bibliographic_fingerprints_match(left, incompatible_authors)
    assert not bibliographic_fingerprints_match(left, incompatible_year)
    assert missing_year is None
    assert not bibliographic_fingerprints_match(left, missing_year)


def test_academic_domain_values_are_immutable_and_protocols_are_importable() -> None:
    identifiers = PaperIdentifiers(doi="10.1000/example")
    with pytest.raises(FrozenInstanceError):
        identifiers.doi = "10.1000/other"  # type: ignore[misc]

    assert PaperRecord is not None
    assert OAResolution is not None
    assert PaperSearchHit is not None
    assert AcademicSearchProvider is not None
    assert OAResolver is not None
