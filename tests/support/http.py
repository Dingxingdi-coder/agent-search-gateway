"""HTTP executor test double for provider adapter contract tests."""

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RecordedRequest:
    method: str
    url: str
    stage: str
    headers: Mapping[str, str] | None
    json_body: object | None


class RecordingJsonExecutor:
    def __init__(self, responses: list[object]) -> None:
        self._responses = list(responses)
        self.requests: list[RecordedRequest] = []

    async def request_json(
        self,
        method: str,
        url: str,
        *,
        stage: str,
        headers: Mapping[str, str] | None = None,
        json_body: object | None = None,
    ) -> object:
        self.requests.append(RecordedRequest(method, url, stage, headers, json_body))
        if not self._responses:
            raise AssertionError("unexpected HTTP request")
        return self._responses.pop(0)
