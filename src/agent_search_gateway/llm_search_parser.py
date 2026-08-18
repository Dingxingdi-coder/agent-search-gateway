"""Restricted Markdown parser for LLM search results."""

from .errors import InputFailure, ParserFailure
from .models import SearchRecord
from .url_normalization import normalize_url

_RESULT_HEADING = "## Result"
_URL_PREFIX = "URL:"
_ABSTRACT_PREFIX = "Abstract:"


def parse_search_markdown(markdown: str) -> list[SearchRecord]:
    lines = markdown.splitlines()
    headings = [index for index, line in enumerate(lines) if line.strip() == _RESULT_HEADING]
    if not headings:
        raise ParserFailure("LLM search response contained no Result blocks")

    records: list[SearchRecord] = []
    for position, start in enumerate(headings):
        end = headings[position + 1] if position + 1 < len(headings) else len(lines)
        record = _parse_block(lines[start + 1 : end])
        if record is not None:
            records.append(record)
    return records


def _parse_block(lines: list[str]) -> SearchRecord | None:
    urls = [
        line.strip()[len(_URL_PREFIX) :].strip()
        for line in lines
        if line.strip().startswith(_URL_PREFIX)
    ]
    abstracts = [
        line.strip()[len(_ABSTRACT_PREFIX) :].strip()
        for line in lines
        if line.strip().startswith(_ABSTRACT_PREFIX)
    ]
    if len(urls) != 1 or len(abstracts) != 1:
        raise ParserFailure("Result block must contain exactly one URL and one Abstract")
    if not urls[0]:
        raise ParserFailure("Result block URL is empty")
    if not abstracts[0]:
        return None
    try:
        normalized = normalize_url(urls[0])
    except InputFailure as exc:
        raise ParserFailure("Result block URL is invalid") from exc
    return SearchRecord(url=normalized, abstract=abstracts[0])
