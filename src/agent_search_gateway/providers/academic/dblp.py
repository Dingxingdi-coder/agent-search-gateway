"""dblp publication search API adapter."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import date

from ...academic.normalization import normalize_dblp_key, normalize_doi
from ..contracts import PaperSearchHit
from .common import AcademicHttpExecutor, protocol_failure, reject_item

_DEFAULT_API_URL = "https://dblp.org/search/publ/api"


class DblpProvider:
    name = "dblp"

    def __init__(
        self,
        executor: AcademicHttpExecutor,
        *,
        api_url: str = _DEFAULT_API_URL,
    ) -> None:
        self._executor = executor
        self._api_url = api_url

    async def search(self, query: str) -> list[PaperSearchHit]:
        body = await self._executor.request_text(
            "GET",
            self._api_url,
            stage="paper_search",
            params={"q": query, "format": "xml", "h": 10},
        )
        try:
            root = ET.fromstring(body)
        except ET.ParseError as exc:
            raise protocol_failure(self.name, "response was not valid XML") from exc
        if root.tag != "result" or root.find("hits") is None:
            raise protocol_failure(self.name, "response XML envelope was invalid")
        hits: list[PaperSearchHit] = []
        for hit in root.findall("./hits/hit"):
            mapped = self._map_hit(hit)
            if mapped is None:
                reject_item(self.name)
            else:
                hits.append(mapped)
        return hits

    def _map_hit(self, hit: ET.Element) -> PaperSearchHit | None:
        info = hit.find("info")
        if info is None:
            return None
        title_element = info.find("title")
        title = self._element_text(title_element)
        url = self._child_text(info, "url")
        source_id = normalize_dblp_key((info.get("key") or "").strip())
        if source_id is None:
            source_id = normalize_dblp_key(url)
        if source_id is None or not title or not url:
            return None
        authors = tuple(
            author
            for element in info.findall("./authors/author")
            if (author := self._element_text(element))
        )
        doi = ""
        for edition in info.findall("ee"):
            candidate = normalize_doi(self._element_text(edition))
            if candidate is not None:
                doi = candidate
                break
        published_date = self._year_date(self._child_text(info, "year"))
        return PaperSearchHit(
            source=self.name,
            source_id=source_id,
            title=title,
            authors=authors,
            doi=doi,
            published_date=published_date,
            url=url,
            venue=self._child_text(info, "venue"),
        )

    @staticmethod
    def _element_text(element: ET.Element | None) -> str:
        if element is None:
            return ""
        return " ".join("".join(element.itertext()).split())

    @classmethod
    def _child_text(cls, parent: ET.Element, name: str) -> str:
        return cls._element_text(parent.find(name))

    @staticmethod
    def _year_date(raw_year: str) -> date | None:
        try:
            year = int(raw_year)
            if year < 1:
                return None
            return date(year, 1, 1)
        except (TypeError, ValueError):
            return None
