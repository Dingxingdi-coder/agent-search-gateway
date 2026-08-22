"""CORE works search API discovery adapter."""

from __future__ import annotations

from ...academic.normalization import normalize_core_id
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

_DEFAULT_API_URL = "https://api.core.ac.uk/v3"


class CoreProvider:
    name = "core"

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
            join_url(self._api_url, "search/works"),
            stage="paper_search",
            headers=self._request_headers(),
            params={"q": query, "limit": 10, "offset": 0},
        )
        envelope = as_mapping(payload)
        results = as_list(envelope.get("results")) if envelope is not None else None
        if results is None:
            raise protocol_failure(self.name, "response results envelope was invalid")
        hits: list[PaperSearchHit] = []
        for item in results:
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
        return {"Authorization": f"Bearer {reveal()}"}

    def _map_item(self, value: object) -> PaperSearchHit | None:
        item = as_mapping(value)
        if item is None:
            return None
        raw_id = item.get("id")
        source_id = normalize_core_id(str(raw_id)) if isinstance(raw_id, str | int) else None
        title = text(item.get("title"))
        if source_id is None or not title:
            return None
        repository = as_mapping(item.get("repository")) or {}
        return PaperSearchHit(
            source=self.name,
            source_id=source_id,
            title=title,
            authors=self._authors(item.get("authors")),
            abstract=text(item.get("abstract")),
            doi=text(item.get("doi")),
            published_date=parse_iso_date(item.get("publishedDate")),
            url=f"https://core.ac.uk/works/{source_id}",
            pdf_url=self._pdf_url(item),
            venue=text(repository.get("name")),
            topics=self._topics(item),
            citation_count=nonnegative_int(item.get("citationCount")),
        )

    @staticmethod
    def _authors(value: object) -> tuple[str, ...]:
        authors: list[str] = []
        for raw_author in as_list(value) or []:
            if isinstance(raw_author, str):
                name = raw_author.strip()
            else:
                author = as_mapping(raw_author)
                name = text(author.get("name")) if author is not None else ""
            if name:
                authors.append(name)
        return tuple(authors)

    @staticmethod
    def _pdf_url(item: object) -> str:
        mapping = as_mapping(item)
        if mapping is None:
            return ""
        direct = text(mapping.get("downloadUrl"))
        if direct:
            return direct
        for raw_url in as_list(mapping.get("fullTextUrls")) or []:
            candidate = text(raw_url)
            if candidate:
                return candidate
        return ""

    @staticmethod
    def _topics(item: object) -> tuple[str, ...]:
        mapping = as_mapping(item)
        if mapping is None:
            return ()
        topics: list[str] = []
        seen: set[str] = set()
        for key in ("subjects", "tags"):
            for raw_topic in as_list(mapping.get(key)) or []:
                topic = text(raw_topic)
                folded = topic.casefold()
                if topic and folded not in seen:
                    seen.add(folded)
                    topics.append(topic)
        return tuple(topics)
