"""Pure normalization helpers for academic paper identity and metadata."""

from __future__ import annotations

import re
import unicodedata
from datetime import date
from urllib.parse import unquote, urlsplit

_DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$", re.IGNORECASE)
_ARXIV_RE = re.compile(r"^(?P<work>\d{4}\.\d{4,5})(?:v\d+)?$", re.IGNORECASE)
_SEMANTIC_SCHOLAR_RE = re.compile(r"^[A-Za-z0-9]{6,64}$")
_OPENALEX_RE = re.compile(r"^W\d+$", re.IGNORECASE)
_CORE_RE = re.compile(r"^[A-Za-z0-9._:-]+$")
_WHITESPACE_RE = re.compile(r"\s+")


def _nfkc(value: str) -> str:
    return unicodedata.normalize("NFKC", value)


def _collapsed(value: str) -> str:
    return _WHITESPACE_RE.sub(" ", _nfkc(value).strip())


def normalize_doi(value: str) -> str | None:
    """Canonicalize a DOI from a bare value, ``doi:`` form, or DOI URL."""

    if not isinstance(value, str):
        return None
    candidate = _nfkc(value).strip()
    if not candidate:
        return None
    if candidate.casefold().startswith("doi:"):
        candidate = candidate[4:].strip()
    else:
        try:
            parsed = urlsplit(candidate)
        except ValueError:
            return None
        if parsed.scheme or parsed.netloc:
            if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
                return None
            if parsed.hostname.casefold() not in {"doi.org", "www.doi.org", "dx.doi.org"}:
                return None
            if parsed.query or parsed.fragment:
                return None
            candidate = unquote(parsed.path.lstrip("/"))
    candidate = candidate.strip()
    if not _DOI_RE.fullmatch(candidate):
        return None
    return candidate.casefold()


def normalize_arxiv_id(value: str) -> str | None:
    """Canonicalize a modern arXiv identifier and remove its version suffix."""

    if not isinstance(value, str):
        return None
    candidate = _nfkc(value).strip()
    if not candidate:
        return None
    if candidate.casefold().startswith("arxiv:"):
        candidate = candidate[6:].strip()
    else:
        try:
            parsed = urlsplit(candidate)
        except ValueError:
            return None
        if parsed.scheme or parsed.netloc:
            if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
                return None
            if parsed.hostname.casefold() not in {"arxiv.org", "www.arxiv.org"}:
                return None
            if parsed.query or parsed.fragment:
                return None
            path = parsed.path.strip("/")
            if path.startswith("abs/"):
                candidate = path[4:]
            elif path.startswith("pdf/") and path.casefold().endswith(".pdf"):
                candidate = path[4:-4]
            else:
                return None
    match = _ARXIV_RE.fullmatch(candidate.strip())
    return match.group("work") if match else None


def normalize_semantic_scholar_id(value: str) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = _nfkc(value).strip()
    return candidate if _SEMANTIC_SCHOLAR_RE.fullmatch(candidate) else None


def normalize_openalex_id(value: str) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = _nfkc(value).strip()
    if not candidate:
        return None
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return None
    if parsed.scheme or parsed.netloc:
        if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
            return None
        if parsed.hostname.casefold() not in {"openalex.org", "www.openalex.org"}:
            return None
        if parsed.query or parsed.fragment:
            return None
        candidate = parsed.path.strip("/")
    if not _OPENALEX_RE.fullmatch(candidate):
        return None
    return candidate.upper()


def normalize_dblp_key(value: str) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = _nfkc(value).strip()
    if not candidate:
        return None
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return None
    if parsed.scheme or parsed.netloc:
        if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
            return None
        if parsed.hostname.casefold() not in {"dblp.org", "www.dblp.org"}:
            return None
        if parsed.query or parsed.fragment:
            return None
        path = parsed.path.strip("/")
        if not path.startswith("rec/"):
            return None
        candidate = path[4:]
        if candidate.casefold().endswith(".html"):
            candidate = candidate[:-5]
    candidate = candidate.strip("/")
    if not candidate or any(character.isspace() for character in candidate):
        return None
    return candidate


def normalize_core_id(value: str) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = _nfkc(value).strip()
    return candidate if candidate and _CORE_RE.fullmatch(candidate) else None


def normalize_source_name(value: str) -> str:
    if not isinstance(value, str):
        return ""
    return _collapsed(value).casefold().replace(" ", "_")


def normalize_source_id(source: str, value: str) -> tuple[str, str] | None:
    """Canonicalize a native identifier and namespace it by provider source."""

    source_name = normalize_source_name(source)
    if not source_name or not isinstance(value, str):
        return None
    normalizer = {
        "arxiv": normalize_arxiv_id,
        "semantic_scholar": normalize_semantic_scholar_id,
        "openalex": normalize_openalex_id,
        "dblp": normalize_dblp_key,
        "crossref": normalize_doi,
        "core": normalize_core_id,
    }.get(source_name)
    normalized = normalizer(value) if normalizer is not None else _nfkc(value).strip()
    if not normalized:
        return None
    return source_name, normalized


def source_identity_key(source: str, value: str) -> str | None:
    identity = normalize_source_id(source, value)
    return None if identity is None else f"{identity[0]}:{identity[1]}"


def normalize_title(value: str) -> str:
    return _collapsed(value).casefold() if isinstance(value, str) else ""


def normalize_author(value: str) -> str:
    return _collapsed(value).casefold() if isinstance(value, str) else ""


def normalize_topic(value: str) -> str:
    return _collapsed(value) if isinstance(value, str) else ""


def publication_year(value: date | None) -> int | None:
    return value.year if isinstance(value, date) else None


def normalized_authors(authors: tuple[str, ...]) -> frozenset[str]:
    return frozenset(
        normalized
        for author in authors
        if (normalized := normalize_author(author))
    )


def bibliographic_fingerprint(
    title: str,
    authors: tuple[str, ...],
    published_date: date | None,
) -> tuple[str, frozenset[str], int] | None:
    """Build strict weak-identity evidence only when all required parts exist."""

    normalized_title = normalize_title(title)
    author_evidence = normalized_authors(authors)
    year = publication_year(published_date)
    if not normalized_title or not author_evidence or year is None:
        return None
    return normalized_title, author_evidence, year


def bibliographic_fingerprints_match(
    left: tuple[str, frozenset[str], int] | None,
    right: tuple[str, frozenset[str], int] | None,
) -> bool:
    if left is None or right is None:
        return False
    left_title, left_authors, left_year = left
    right_title, right_authors, right_year = right
    return (
        left_title == right_title
        and left_year == right_year
        and bool(left_authors & right_authors)
    )


def weak_bibliographic_match(
    *,
    left_title: str,
    left_authors: tuple[str, ...],
    left_published_date: date | None,
    right_title: str,
    right_authors: tuple[str, ...],
    right_published_date: date | None,
) -> bool:
    return bibliographic_fingerprints_match(
        bibliographic_fingerprint(left_title, left_authors, left_published_date),
        bibliographic_fingerprint(right_title, right_authors, right_published_date),
    )
