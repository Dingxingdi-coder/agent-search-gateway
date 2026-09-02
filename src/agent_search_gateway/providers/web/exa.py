"""Exa search and contents adapter."""

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


class ExaAdapter:
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
        return {"x-api-key": self._secret.reveal()}

    async def search(self, query: str) -> list[KeywordSearchHit]:
        payload = await self._http.request_json(
            "POST",
            endpoint(self._api_url, "/search"),
            stage="search",
            headers=self._headers,
            json_body={"query": query, "contents": {"text": True, "highlights": True}},
        )
        root = require_object(payload, self.name, "search", "response")
        results = require_list(root.get("results"), self.name, "search", "results")
        hits: list[KeywordSearchHit] = []
        for item in results:
            try:
                result = require_object(item, self.name, "search", "result")
                url = non_empty_string(result.get("url"), self.name, "search", "result.url")
                title = optional_string(result.get("title"), self.name, "search", "result.title")
                text = optional_string(result.get("text"), self.name, "search", "result.text")
                snippet = self._snippet(result, title)
                hits.append(
                    KeywordSearchHit(
                        url=url,
                        title=title,
                        snippet=snippet,
                        raw_content=text,
                        content=text,
                    )
                )
            except ExecutionFailure:
                continue
        return hits

    async def fetch(self, url: NormalizedURL) -> URLFetchCandidate:
        payload = await self._http.request_json(
            "POST",
            endpoint(self._api_url, "/contents"),
            stage="fetch",
            headers=self._headers,
            json_body={"urls": [str(url)], "text": True},
        )
        root = require_object(payload, self.name, "fetch", "response")
        self._require_success_status(root, url)
        results = require_list(root.get("results"), self.name, "fetch", "results")
        for item in results:
            result = require_object(item, self.name, "fetch", "result")
            if normalized_match(result.get("url"), url, self.name, "fetch"):
                text = non_empty_string(result.get("text"), self.name, "fetch", "result.text")
                return URLFetchCandidate(raw_content=text, content=text)
        raise failure(self.name, "fetch", "matching contents result was not returned")

    def _snippet(self, result: dict[str, object], title: str) -> str:
        highlights_value = result.get("highlights")
        if highlights_value is not None:
            highlights = require_list(highlights_value, self.name, "search", "result.highlights")
            if highlights:
                first = optional_string(highlights[0], self.name, "search", "result.highlights[0]")
                if first.strip():
                    return first
        summary = optional_string(result.get("summary"), self.name, "search", "result.summary")
        return summary.strip() or title

    def _require_success_status(self, root: dict[str, object], url: NormalizedURL) -> None:
        statuses = require_list(root.get("statuses"), self.name, "fetch", "statuses")
        for item in statuses:
            status = require_object(item, self.name, "fetch", "status")
            if normalized_match(status.get("id"), url, self.name, "fetch"):
                state = non_empty_string(status.get("status"), self.name, "fetch", "status.status")
                if state != "success":
                    raise failure(self.name, "fetch", "provider reported per-URL failure")
                return
        raise failure(self.name, "fetch", "matching per-URL status was not returned")
