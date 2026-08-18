"""Pure prompt builders for gateway LLM stages."""

from ..providers.contracts import ChatMessage


def judge_messages(candidate: str) -> tuple[ChatMessage, ...]:
    return (
        {
            "role": "system",
            "content": (
                "Decide whether the supplied text is usable main-body content for the target "
                "web page. Return JSON with boolean field ok and optional short string field "
                "reason."
            ),
        },
        {"role": "user", "content": f"Candidate content:\n{candidate}"},
    )


def safety_messages(content: str) -> tuple[ChatMessage, ...]:
    return (
        {
            "role": "system",
            "content": (
                "Decide whether the supplied final page content is safe to return. "
                "Return JSON with boolean field ok and optional short string field reason."
            ),
        },
        {"role": "user", "content": f"Final content:\n{content}"},
    )


def content_clean_messages(raw_content: str) -> tuple[ChatMessage, ...]:
    return (
        {
            "role": "system",
            "content": (
                "Return only the cleaned, readable main content from the supplied raw page "
                "text."
            ),
        },
        {"role": "user", "content": raw_content},
    )


def focus_summary_messages(content: str, focus: str) -> tuple[ChatMessage, ...]:
    normalized_focus = focus.strip()
    return (
        {
            "role": "system",
            "content": "Summarize only the supplied page content, emphasizing the requested focus.",
        },
        {
            "role": "user",
            "content": f"Focus: {normalized_focus}\n\nPage content:\n{content}",
        },
    )


def llm_search_messages(prompt: str) -> tuple[ChatMessage, ...]:
    return (
        {
            "role": "system",
            "content": (
                "Find relevant web results. Return only repeated blocks in this exact "
                "restricted format:\n## Result\nURL: https://example.com/page\n"
                "Abstract: concise non-empty summary\n"
                "Do not use Markdown links or other result formats."
            ),
        },
        {"role": "user", "content": prompt.strip()},
    )
