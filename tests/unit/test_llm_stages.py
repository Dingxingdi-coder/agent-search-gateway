import pytest

from agent_search_gateway.errors import ErrorCode, ExecutionFailure
from agent_search_gateway.llm.stages import LLMStages
from agent_search_gateway.models import LLMInvocation, StageDecision
from tests.support.fakes import FakeLLMClient


async def test_llm_stages_use_resolved_invocations_and_validate_outputs() -> None:
    judge_invocation = LLMInvocation("judge", "judge-model", {"stage": "judge"})
    safety_invocation = LLMInvocation("safety", "safety-model", {"stage": "safety"})
    clean_invocation = LLMInvocation("clean", "clean-model", {"stage": "clean"})
    focus_invocation = LLMInvocation("focus", "focus-model", {"stage": "focus"})
    search_invocation = LLMInvocation("search", "search-model", {"stage": "search"})

    judge_client = FakeLLMClient("judge", json_result={"ok": True})
    safety_client = FakeLLMClient("safety", json_result={"ok": False, "reason": "unsafe"})
    clean_client = FakeLLMClient("clean", text_result="  cleaned content  ")
    focus_client = FakeLLMClient("focus", text_result="  focused summary  ")
    search_client = FakeLLMClient(
        "search",
        text_result="## Result\nURL: https://example.com\nAbstract: result",
    )
    stages = LLMStages(
        {
            "judge": judge_client,
            "safety": safety_client,
            "clean": clean_client,
            "focus": focus_client,
            "search": search_client,
        },
        judge=judge_invocation,
        safety=safety_invocation,
        content_clean=clean_invocation,
        focus_summary=focus_invocation,
    )

    assert await stages.safety("final content") == StageDecision(False, "unsafe")
    assert await stages.content_clean("raw content") == "cleaned content"
    assert await stages.focus_summary("final content", "  pricing details  ") == "focused summary"
    markdown = await stages.llm_search_markdown(search_invocation, "find docs")
    assert markdown.startswith("## Result")

    assert safety_client.json_calls[0][0] == safety_invocation
    assert clean_client.text_calls[0][0] == clean_invocation
    assert focus_client.text_calls[0][0] == focus_invocation
    assert search_client.text_calls[0][0] == search_invocation

    focus_prompt = focus_client.text_calls[0][1][-1]["content"]
    assert "pricing details" in focus_prompt
    assert "final content" in focus_prompt
    search_prompt = "\n".join(message["content"] for message in search_client.text_calls[0][1])
    assert "## Result" in search_prompt
    assert "URL:" in search_prompt
    assert "Abstract:" in search_prompt

    safety_client.json_result = {"ok": "yes"}
    with pytest.raises(ExecutionFailure) as safety_error:
        await stages.safety("content")
    assert safety_error.value.code is ErrorCode.LLM_STAGE_FAILED

    clean_client.text_result = "   "
    with pytest.raises(ExecutionFailure) as clean_error:
        await stages.content_clean("raw")
    assert clean_error.value.code is ErrorCode.LLM_STAGE_FAILED

    focus_client.text_result = ""
    with pytest.raises(ExecutionFailure) as focus_error:
        await stages.focus_summary("content", "topic")
    assert focus_error.value.code is ErrorCode.LLM_STAGE_FAILED

    search_client.text_result = "   "
    with pytest.raises(ExecutionFailure) as search_error:
        await stages.llm_search_markdown(search_invocation, "find")
    assert search_error.value.code is ErrorCode.LLM_STAGE_FAILED

    upstream_failure = ExecutionFailure(ErrorCode.ALL_PROVIDERS_FAILED, "upstream")
    focus_client.failure = upstream_failure
    with pytest.raises(ExecutionFailure) as upstream:
        await stages.focus_summary("content", "topic")
    assert upstream.value is upstream_failure
    assert judge_client.text_calls == []
