"""TinyFish search and fetch adapter using separate public endpoints."""

from urllib.parse import urlencode

from ...errors import ExecutionFailure
from ...observability import SecretValue
from ...providers.contracts import KeywordSearchHit, URLFetchCandidate
from ...url_normalization import NormalizedURL
from .common import (
    JsonRequester,
    failure,
    non_empty_string,
    normalized_match,
    optional_string,
    require_list,
    require_object,
)


class TinyFishAdapter:
    def __init__(
        self,
        *,
        name: str,
        search_api_url: str,
        fetch_api_url: str,
        secret: SecretValue,
        http_executor: JsonRequester,
    ) -> None:
        self.name = name
        self._search_api_url = search_api_url.rstrip("/")
        self._fetch_api_url = fetch_api_url.rstrip("/")
        self._secret = secret
        self._http = http_executor

    @property
    def _headers(self) -> dict[str, str]:
        return {"X-API-Key": self._secret.reveal()}

    async def search(self, query: str) -> list[KeywordSearchHit]:
        request_url = f"{self._search_api_url}?{urlencode({'query': query})}"
        payload = await self._http.request_json(
            "GET",
            request_url,
            stage="search",
            headers=self._headers,
        )
        root = require_object(payload, self.name, "search", "response")
        results = require_list(root.get("results"), self.name, "search", "results")
        hits: list[KeywordSearchHit] = []
        for item in results:
            try:
                result = require_object(item, self.name, "search", "result")
                url = non_empty_string(result.get("url"), self.name, "search", "result.url")
                hits.append(
                    KeywordSearchHit(
                        url=url,
                        title=optional_string(
                            result.get("title"), self.name, "search", "result.title"
                        ),
                        snippet=optional_string(
                            result.get("snippet"), self.name, "search", "result.snippet"
                        ),
                    )
                )
            except ExecutionFailure:
                continue
        return hits

    async def fetch(self, url: NormalizedURL) -> URLFetchCandidate:
        payload = await self._http.request_json(
            "POST",
            self._fetch_api_url,
            stage="fetch",
            headers=self._headers,
            json_body={
                "urls": [str(url)],
                "format": "markdown",
                "links": False,
                "image_links": False,
            },
        )
        root = require_object(payload, self.name, "fetch", "response")
        results = require_list(root.get("results"), self.name, "fetch", "results")
        for item in results:
            result = require_object(item, self.name, "fetch", "result")
            if normalized_match(result.get("url"), url, self.name, "fetch"):
                text = non_empty_string(result.get("text"), self.name, "fetch", "result.text")
                return URLFetchCandidate(raw_content=text, content=text)

        errors_value = root.get("errors", [])
        errors = require_list(errors_value, self.name, "fetch", "errors")
        for item in errors:
            error = require_object(item, self.name, "fetch", "error")
            if normalized_match(error.get("url"), url, self.name, "fetch"):
                raise failure(self.name, "fetch", "provider reported per-URL failure")
        raise failure(self.name, "fetch", "matching fetch result was not returned")
