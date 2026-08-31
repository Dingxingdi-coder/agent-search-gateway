"""Tavily search and extract adapter."""

from ...errors import ExecutionFailure
from ...observability import SecretValue
from ...providers.contracts import KeywordSearchHit, URLFetchCandidate
from ...url_normalization import NormalizedURL
from .common import (
    JsonRequester,
    endpoint,
    failure,
    non_empty_string,
    normalized_match,
    optional_string,
    require_list,
    require_object,
)


class TavilyAdapter:
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
            endpoint(self._api_url, "/search"),
            stage="search",
            headers=self._headers,
            json_body={"query": query, "include_raw_content": "markdown"},
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
                            result.get("content"), self.name, "search", "result.content"
                        ),
                        raw_content=optional_string(
                            result.get("raw_content"),
                            self.name,
                            "search",
                            "result.raw_content",
                        ),
                    )
                )
            except ExecutionFailure:
                continue
        return hits

    async def fetch(self, url: NormalizedURL) -> URLFetchCandidate:
        payload = await self._http.request_json(
            "POST",
            endpoint(self._api_url, "/extract"),
            stage="fetch",
            headers=self._headers,
            json_body={"urls": [str(url)]},
        )
        try:
            root = require_object(payload, self.name, "fetch", "response")
            results = require_list(root.get("results"), self.name, "fetch", "results")
        except ExecutionFailure as exc:
            reason = "invalid_results_envelope"
            raise failure(self.name, "fetch", reason, reason_code=reason) from exc

        empty_match_seen = False
        for item in results:
            try:
                result = require_object(item, self.name, "fetch", "result")
                if not normalized_match(result.get("url"), url, self.name, "fetch"):
                    continue
            except ExecutionFailure:
                continue
            raw = result.get("raw_content")
            if not isinstance(raw, str) or not raw.strip():
                empty_match_seen = True
                continue
            return URLFetchCandidate(raw_content=raw)

        reason = "empty_raw_content" if empty_match_seen else "no_matching_result"
        raise failure(self.name, "fetch", reason, reason_code=reason)
