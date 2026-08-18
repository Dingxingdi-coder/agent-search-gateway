"""Brave web search adapter."""

from urllib.parse import urlencode

from ...errors import ExecutionFailure
from ...observability import SecretValue
from ...providers.contracts import KeywordSearchHit
from .common import (
    JsonRequester,
    endpoint,
    non_empty_string,
    optional_string,
    require_list,
    require_object,
)


class BraveAdapter:
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

    async def search(self, query: str) -> list[KeywordSearchHit]:
        base = endpoint(self._api_url, "/res/v1/web/search")
        request_url = f"{base}?{urlencode({'q': query, 'count': 10})}"
        payload = await self._http.request_json(
            "GET",
            request_url,
            stage="search",
            headers={"X-Subscription-Token": self._secret.reveal()},
        )
        root = require_object(payload, self.name, "search", "response")
        web_value = root.get("web")
        if web_value is None:
            return []
        web = require_object(web_value, self.name, "search", "web")
        results_value = web.get("results")
        if results_value is None:
            return []
        results = require_list(results_value, self.name, "search", "web.results")
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
                            result.get("description"),
                            self.name,
                            "search",
                            "result.description",
                        ),
                    )
                )
            except ExecutionFailure:
                continue
        return hits
