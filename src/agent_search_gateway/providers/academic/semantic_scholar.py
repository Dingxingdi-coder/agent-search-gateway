"""Semantic Scholar Graph API discovery adapter."""

from __future__ import annotations

from ...observability import SecretValue
from ..contracts import PaperSearchHit
from .common import (
    AcademicHttpExecutor,
    as_list,
    as_mapping,
    join_url,
    nonnegative_int,
    parse_iso_date,
    protocol_failure,
    reject_item,
    text,
)

_DEFAULT_API_URL = "https://api.semanticscholar.org/graph/v1"
_FIELDS = (
    "title,abstract,citationCount,authors,url,publicationDate,"
    "externalIds,fieldsOfStudy,openAccessPdf"
)


class SemanticScholarProvider:
    name = "semantic_scholar"

    def __init__(
        self,
        executor: AcademicHttpExecutor,
        *,
        api_url: str = _DEFAULT_API_URL,
        api_key: SecretValue | None = None,
    ) -> None:
        self._executor = executor
        self._api_url = api_url
        self._credential = api_key

    async def search(self, query: str) -> list[PaperSearchHit]:
        payload = await self._executor.request_json(
            "GET",
            join_url(self._api_url, "paper/search"),
            stage="paper_search",
            headers=self._request_headers(),
            params={"query": query, "limit": 10, "fields": _FIELDS},
        )
        envelope = as_mapping(payload)
        data = as_list(envelope.get("data")) if envelope is not None else None
        if data is None:
            raise protocol_failure(self.name, "response data envelope was invalid")
        hits: list[PaperSearchHit] = []
        for item in data:
            mapped = self._map_item(item)
            if mapped is None:
                reject_item(self.name)
            else:
                hits.append(mapped)
        return hits

    def _request_headers(self) -> dict[str, str] | None:
        if self._credential is None:
            return None
        reveal = self._credential.reveal
        return {"x-api-key": reveal()}

    def _map_item(self, value: object) -> PaperSearchHit | None:
        item = as_mapping(value)
        if item is None:
            return None
        paper_id = text(item.get("paperId"))
        title = text(item.get("title"))
        url = text(item.get("url"))
        if not paper_id or not title or not url:
            return None
        external = as_mapping(item.get("externalIds")) or {}
        authors_raw = as_list(item.get("authors")) or []
        authors = tuple(
            name
            for author in authors_raw
            if (mapping := as_mapping(author)) is not None
            and (name := text(mapping.get("name")))
        )
        topics = tuple(
            topic
            for raw_topic in (as_list(item.get("fieldsOfStudy")) or [])
            if (topic := text(raw_topic))
        )
        oa = as_mapping(item.get("openAccessPdf"))
        pdf_url = text(oa.get("url")) if oa is not None else ""
        return PaperSearchHit(
            source=self.name,
            source_id=paper_id,
            title=title,
            authors=authors,
            abstract=text(item.get("abstract")),
            doi=text(external.get("DOI")),
            arxiv_id=text(external.get("ArXiv")),
            published_date=parse_iso_date(item.get("publicationDate")),
            url=url,
            pdf_url=pdf_url,
            topics=topics,
            citation_count=nonnegative_int(item.get("citationCount")),
            is_open_access=True if pdf_url else None,
        )
