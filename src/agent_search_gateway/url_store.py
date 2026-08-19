"""In-memory five-field URL state machine."""

from dataclasses import replace

from .models import URLRecord
from .url_normalization import NormalizedURL


def _first_non_empty(existing: str, candidate: str) -> str:
    return existing if existing else candidate


class URLStore:
    def __init__(self) -> None:
        self._records: dict[NormalizedURL, URLRecord] = {}

    def admit(
        self,
        url: NormalizedURL,
        abstract: str,
        *,
        raw_content: str = "",
        content: str = "",
    ) -> URLRecord:
        normalized_abstract = abstract.strip()
        if not normalized_abstract:
            raise ValueError("URL records require a non-empty abstract")

        existing = self._records.get(url)
        if existing is None:
            record = URLRecord(
                url=url,
                raw_content=raw_content,
                content=content,
                abstract=normalized_abstract,
            )
        else:
            record = replace(
                existing,
                raw_content=_first_non_empty(existing.raw_content, raw_content),
                content=_first_non_empty(existing.content, content),
                abstract=_first_non_empty(existing.abstract, normalized_abstract),
            )
        self._records[url] = record
        return record

    def merge_body(
        self,
        url: NormalizedURL,
        *,
        raw_content: str = "",
        content: str = "",
    ) -> URLRecord:
        existing = self._require(url)
        record = replace(
            existing,
            raw_content=_first_non_empty(existing.raw_content, raw_content),
            content=_first_non_empty(existing.content, content),
        )
        self._records[url] = record
        return record

    def mark_unavailable(self, url: NormalizedURL) -> URLRecord:
        existing = self._require(url)
        if not existing.available:
            return existing
        record = replace(existing, available=False)
        self._records[url] = record
        return record

    def get(self, url: NormalizedURL) -> URLRecord | None:
        return self._records.get(url)

    def _require(self, url: NormalizedURL) -> URLRecord:
        record = self._records.get(url)
        if record is None:
            raise KeyError(str(url))
        return record
