"""Resolved prompt-level LLM stages."""

from collections.abc import Mapping

from ..errors import ErrorCode, ExecutionFailure
from ..models import LLMInvocation, StageDecision
from ..providers.contracts import LLMClient
from .prompts import (
    content_clean_messages,
    focus_summary_messages,
    judge_messages,
    llm_search_messages,
    safety_messages,
)


def cheap_check(candidate: str) -> bool:
    return bool(candidate.strip())


class LLMStages:
    def __init__(
        self,
        clients: Mapping[str, LLMClient],
        *,
        judge: LLMInvocation,
        safety: LLMInvocation,
        content_clean: LLMInvocation,
        focus_summary: LLMInvocation,
    ) -> None:
        self._clients = dict(clients)
        self._judge = judge
        self._safety = safety
        self._content_clean = content_clean
        self._focus_summary = focus_summary

    async def judge(self, candidate: str) -> StageDecision:
        client = self._client(self._judge.provider)
        payload = await client.complete_json(self._judge, judge_messages(candidate))
        return self._parse_decision(payload)

    async def safety(self, content: str) -> StageDecision:
        client = self._client(self._safety.provider)
        payload = await client.complete_json(self._safety, safety_messages(content))
        return self._parse_decision(payload)

    async def content_clean(self, raw_content: str) -> str:
        client = self._client(self._content_clean.provider)
        text = await client.complete_text(
            self._content_clean,
            content_clean_messages(raw_content),
        )
        return self._require_non_empty(text, "content-clean")

    async def focus_summary(self, content: str, focus: str) -> str:
        normalized_focus = focus.strip()
        if not normalized_focus:
            raise ExecutionFailure(ErrorCode.LLM_STAGE_FAILED, "focus must be non-empty")
        client = self._client(self._focus_summary.provider)
        text = await client.complete_text(
            self._focus_summary,
            focus_summary_messages(content, normalized_focus),
        )
        return self._require_non_empty(text, "focus-summary")

    async def llm_search_markdown(self, invocation: LLMInvocation, prompt: str) -> str:
        client = self._client(invocation.provider)
        text = await client.complete_text(invocation, llm_search_messages(prompt))
        return self._require_non_empty(text, "llm-search")

    def _client(self, provider: str) -> LLMClient:
        client = self._clients.get(provider)
        if client is None:
            raise ExecutionFailure(
                ErrorCode.LLM_STAGE_FAILED,
                f"LLM provider is not initialized: {provider}",
            )
        return client

    @staticmethod
    def _require_non_empty(text: str, stage: str) -> str:
        normalized = text.strip()
        if not normalized:
            raise ExecutionFailure(
                ErrorCode.LLM_STAGE_FAILED,
                f"{stage} returned empty text",
            )
        return normalized

    @staticmethod
    def _parse_decision(payload: Mapping[str, object]) -> StageDecision:
        ok = payload.get("ok")
        if not isinstance(ok, bool):
            raise ExecutionFailure(
                ErrorCode.LLM_STAGE_FAILED,
                "LLM decision response requires boolean ok",
            )
        reason = payload.get("reason", "")
        if not isinstance(reason, str):
            raise ExecutionFailure(
                ErrorCode.LLM_STAGE_FAILED,
                "LLM decision reason must be a string",
            )
        return StageDecision(ok=ok, reason=reason.strip())
