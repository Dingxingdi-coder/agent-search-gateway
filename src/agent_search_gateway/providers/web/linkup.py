"""Linkup search and fetch adapter."""

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


class LinkupAdapter:
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
            endpoint(self._api_url, "/v1/search"),
            stage="search",
            headers=self._headers,
            json_body={"q": query, "depth": "standard", "outputType": "searchResults"},
        )
        root = require_object(payload, self.name, "search", "response")
        results = require_list(root.get("results"), self.name, "search", "results")
        hits: list[KeywordSearchHit] = []
        for item in results:
            result = require_object(item, self.name, "search", "result")
            hits.append(
                KeywordSearchHit(
                    url=non_empty_string(result.get("url"), self.name, "search", "result.url"),
                    title=non_empty_string(result.get("name"), self.name, "search", "result.name"),
                    snippet=non_empty_string(
                        result.get("content"), self.name, "search", "result.content"
                    ),
                )
            )
        return hits

    async def fetch(self, url: NormalizedURL) -> URLFetchCandidate:
        payload = await self._http.request_json(
            "POST",
            endpoint(self._api_url, "/v1/fetch"),
            stage="fetch",
            headers=self._headers,
            json_body={
                "url": str(url),
                "includeRawContent": True,
                "extractImages": False,
            },
        )
        root = require_object(payload, self.name, "fetch", "response")
        markdown = optional_string(root.get("markdown"), self.name, "fetch", "markdown")
        raw_content = optional_string(root.get("rawContent"), self.name, "fetch", "rawContent")
        raw = raw_content or markdown
        if not raw.strip():
            raise failure(self.name, "fetch", "page body is empty")
        return URLFetchCandidate(raw_content=raw, content=markdown)
