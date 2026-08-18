import pytest

from agent_search_gateway.errors import ParserFailure
from agent_search_gateway.llm_search_parser import parse_search_markdown


def test_restricted_markdown_parser_accepts_only_result_blocks_and_drops_empty_abstracts() -> None:
    markdown = """
[Outside](https://ignore.example)

## Result
URL: https://EXAMPLE.com/One?Q=1#frag
Abstract: First result

## Result
URL: https://example.com/two
Abstract:

Some [other link](https://ignore-too.example)

## Result
URL: http://Example.COM/three
Abstract: Third result
"""

    records = parse_search_markdown(markdown)
    assert [(str(record.url), record.abstract) for record in records] == [
        ("https://example.com/One?Q=1#frag", "First result"),
        ("http://example.com/three", "Third result"),
    ]


@pytest.mark.parametrize(
    "markdown",
    [
        "plain text with https://example.com",
        "## Result\nAbstract: missing url",
        "## Result\nURL: https://example.com\n",
        "## Result\nURL: https://example.com\nURL: https://example.org\nAbstract: duplicate",
        "## Result\nURL: ftp://example.com\nAbstract: invalid url",
    ],
)
def test_restricted_markdown_parser_rejects_malformed_required_structure(markdown: str) -> None:
    with pytest.raises(ParserFailure):
        parse_search_markdown(markdown)
