"""Firecrawl v2 search and scrape adapter."""

from ...observability import SecretValue
from ...providers.contracts import KeywordSearchHit, URLFetchCandidate
from ...url_normalization import NormalizedURL
from .common import (
    JsonRequester,
    endpoint,
    failure,
    non_empty_string,
    optional_string,
    require_list,
    require_object,
)


def _v2_endpoint(api_url: str, suffix: str) -> str:
    base = api_url.rstrip("/")
    if base.endswith("/v2"):
        return endpoint(base, suffix)
    return endpoint(base, f"/v2/{suffix.lstrip('/')}")


def _page_body(
    result: dict[str, object],
    provider: str,
    stage: str,
) -> URLFetchCandidate:
    markdown = optional_string(result.get("markdown"), provider, stage, "markdown")
    raw_html = optional_string(result.get("rawHtml"), provider, stage, "rawHtml")
    raw = raw_html or markdown
    if not raw.strip():
        raise failure(provider, stage, "page body is empty")
    return URLFetchCandidate(raw_content=raw, content=markdown)


class FirecrawlAdapter:
    def __init__(
        self,
        *,
        name: str,
        api_url: str,
        secret: SecretValue,
        http_executor: JsonRequester,
    ) -> None:
        self.name = name
        self._api_url = api_url
        self._secret = secret
        self._http = http_executor

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._secret.reveal()}"}

    async def search(self, query: str) -> list[KeywordSearchHit]:
        payload = await self._http.request_json(
            "POST",
            _v2_endpoint(self._api_url, "search"),
            stage="search",
            headers=self._headers,
            json_body={
                "query": query,
                "sources": ["web"],
                "scrapeOptions": {"formats": ["markdown", "rawHtml"]},
            },
        )
        root = self._successful_root(payload, "search")
        data = require_object(root.get("data"), self.name, "search", "data")
        web = require_list(data.get("web"), self.name, "search", "data.web")
        hits: list[KeywordSearchHit] = []
        for item in web:
            result = require_object(item, self.name, "search", "result")
            body = _page_body(result, self.name, "search")
            hits.append(
                KeywordSearchHit(
                    url=non_empty_string(result.get("url"), self.name, "search", "result.url"),
                    title=non_empty_string(
                        result.get("title"), self.name, "search", "result.title"
                    ),
                    snippet=non_empty_string(
                        result.get("description"),
                        self.name,
                        "search",
                        "result.description",
                    ),
                    raw_content=body.raw_content,
                    content=body.content,
                )
            )
        return hits

    async def fetch(self, url: NormalizedURL) -> URLFetchCandidate:
        payload = await self._http.request_json(
            "POST",
            _v2_endpoint(self._api_url, "scrape"),
            stage="fetch",
            headers=self._headers,
            json_body={"url": str(url), "formats": ["markdown", "rawHtml"]},
        )
        root = self._successful_root(payload, "fetch")
        data = require_object(root.get("data"), self.name, "fetch", "data")
        return _page_body(data, self.name, "fetch")

    def _successful_root(self, payload: object, stage: str) -> dict[str, object]:
        root = require_object(payload, self.name, stage, "response")
        if root.get("success") is not True:
            raise failure(self.name, stage, "provider reported failure")
        return root
