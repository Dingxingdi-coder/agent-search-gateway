from agent_search_gateway.llm.stages import LLMStages
from agent_search_gateway.models import LLMInvocation
from tests.support.fakes import FakeLLMClient
from tests.support.logging import structured_test_logger


async def test_llm_paper_stage_uses_separate_strict_prompt_and_safe_logging() -> None:
    logger, stream = structured_test_logger("tests.llm.paper-stage")
    invocation = LLMInvocation("search", "paper-model", {})
    output = "MODEL_PAPER_OUTPUT_SENTINEL"
    client = FakeLLMClient("search", text_result=output)
    stages = LLMStages(
        {"search": client},
        judge=invocation,
        safety=invocation,
        content_clean=invocation,
        focus_summary=invocation,
        logger=logger,
    )

    assert await stages.llm_paper_search_markdown(
        invocation,
        "USER_PAPER_PROMPT_SENTINEL",
    ) == output

    messages = client.text_calls[0][1]
    prompt = "\n".join(message["content"] for message in messages)
    for field in (
        "## Paper",
        "Title:",
        "Authors:",
        "Abstract:",
        "DOI:",
        "arXiv:",
        "Published:",
        "Updated:",
        "URL:",
        "PDF:",
        "Venue:",
        "Topics:",
        "Citations:",
        "Open Access:",
        "OA Status:",
        "License:",
    ):
        assert field in prompt
    assert "## Result" not in messages[0]["content"]

    logged = stream.getvalue()
    assert "stage=llm_paper_search" in logged
    assert "USER_PAPER_PROMPT_SENTINEL" not in logged
    assert output not in logged
