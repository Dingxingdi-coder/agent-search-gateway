import pytest

from agent_search_gateway.errors import ErrorCode, ExecutionFailure
from agent_search_gateway.llm.stages import LLMStages, cheap_check
from agent_search_gateway.models import LLMInvocation, StageDecision
from tests.support.fakes import FakeLLMClient


async def test_judge_distinguishes_semantic_rejection_from_execution_failure() -> None:
    assert cheap_check("") is False
    assert cheap_check("   \n") is False
    assert cheap_check("<html>anything non-empty</html>") is True

    invocation = LLMInvocation("judge-provider", "judge-model", {"temperature": 0})
    client = FakeLLMClient("judge-provider", json_result={"ok": False, "reason": "not body"})
    stages = LLMStages(
        {"judge-provider": client},
        judge=invocation,
        safety=invocation,
        content_clean=invocation,
        focus_summary=invocation,
    )

    assert await stages.judge("candidate body") == StageDecision(False, "not body")
    assert client.json_calls[0][0] == invocation
    assert "candidate body" in client.json_calls[0][1][-1]["content"]

    client.json_result = {"ok": True}
    assert await stages.judge("usable") == StageDecision(True, "")

    execution = ExecutionFailure(ErrorCode.ALL_PROVIDERS_FAILED, "client failed")
    client.failure = execution
    with pytest.raises(ExecutionFailure) as caught:
        await stages.judge("candidate")
    assert caught.value is execution

    client.failure = None
    invalid_payloads: tuple[dict[str, object], ...] = ({}, {"ok": "yes"}, {"ok": 1})
    for invalid in invalid_payloads:
        client.json_result = invalid
        with pytest.raises(ExecutionFailure) as invalid_caught:
            await stages.judge("candidate")
        assert invalid_caught.value.code is ErrorCode.LLM_STAGE_FAILED
